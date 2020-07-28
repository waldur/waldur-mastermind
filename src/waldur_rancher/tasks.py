import logging

from celery import shared_task
from django.conf import settings
from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.exceptions import RuntimeStateException
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.common import utils as common_utils
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.views import InstanceViewSet
from waldur_rancher.utils import SyncUser

from . import exceptions, models, utils

logger = logging.getLogger(__name__)


class CreateNodeTask(core_tasks.Task):
    def execute(self, instance, user_id):
        node = instance
        content_type = ContentType.objects.get_for_model(
            openstack_tenant_models.Instance
        )
        flavor = node.initial_data['flavor']
        system_volume_size = node.initial_data['system_volume_size']
        system_volume_type = node.initial_data.get('system_volume_type')
        data_volumes = node.initial_data.get('data_volumes', [])
        image = node.initial_data['image']
        subnet = node.initial_data['subnet']
        security_groups = node.initial_data['security_groups']
        tenant_spl = node.initial_data['tenant_service_project_link']
        user = auth.get_user_model().objects.get(pk=user_id)
        ssh_public_key = node.initial_data.get('ssh_public_key')

        post_data = {
            'name': node.name,
            'flavor': reverse('openstacktenant-flavor-detail', kwargs={'uuid': flavor}),
            'image': reverse('openstacktenant-image-detail', kwargs={'uuid': image}),
            'service_project_link': reverse(
                'openstacktenant-spl-detail', kwargs={'pk': tenant_spl}
            ),
            'system_volume_size': system_volume_size,
            'system_volume_type': system_volume_type
            and reverse(
                'openstacktenant-volume-type-detail',
                kwargs={'uuid': system_volume_type},
            ),
            'data_volumes': [
                {
                    'size': volume['size'],
                    'volume_type': volume.get('volume_type')
                    and reverse(
                        'openstacktenant-volume-type-detail',
                        kwargs={'uuid': volume.get('volume_type')},
                    ),
                }
                for volume in data_volumes
            ],
            'security_groups': [
                {'url': reverse('openstacktenant-sgp-detail', kwargs={'uuid': group})}
                for group in security_groups
            ],
            'internal_ips_set': [
                {
                    'subnet': reverse(
                        'openstacktenant-subnet-detail', kwargs={'uuid': subnet}
                    )
                }
            ],
            'user_data': utils.format_node_cloud_config(node),
        }

        if node.cluster.settings.get_option('allocate_floating_ip_to_all_nodes'):
            post_data['floating_ips'] = [
                {
                    'subnet': reverse(
                        'openstacktenant-subnet-detail', kwargs={'uuid': subnet}
                    )
                }
            ]

        if ssh_public_key:
            post_data['ssh_public_key'] = reverse(
                'sshpublickey-detail', kwargs={'uuid': ssh_public_key},
            )

        view = InstanceViewSet.as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)

        if response.status_code != status.HTTP_201_CREATED:
            raise exceptions.RancherException(response.data)

        instance_uuid = response.data['uuid']
        instance = openstack_tenant_models.Instance.objects.get(uuid=instance_uuid)
        node.content_type = content_type
        node.object_id = instance.id
        node.state = models.Node.States.CREATING
        node.save()

        resource_imported.send(
            sender=instance.__class__, instance=instance,
        )

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Create nodes for k8s cluster "%s".' % instance


class DeleteNodeTask(core_tasks.Task):
    def execute(self, instance, user_id):
        node = instance
        user = auth.get_user_model().objects.get(pk=user_id)

        if node.instance:
            view = InstanceViewSet.as_view({'delete': 'force_destroy'})
            response = common_utils.delete_request(
                view,
                user,
                uuid=node.instance.uuid.hex,
                query_params={'delete_volumes': True},
            )

            if response.status_code != status.HTTP_202_ACCEPTED:
                raise exceptions.RancherException(response.data)
        else:
            backend = node.cluster.get_backend()
            backend.delete_node(node)


@shared_task
def pull_cluster_nodes(cluster_id):
    cluster = models.Cluster.objects.get(id=cluster_id)
    backend = cluster.get_backend()

    if cluster.node_set.filter(backend_id='').exists():
        backend_nodes = backend.get_cluster_nodes(cluster.backend_id)

        for backend_node in backend_nodes:
            if cluster.node_set.filter(name=backend_node['name']).exists():
                node = cluster.node_set.get(name=backend_node['name'])
                node.backend_id = backend_node['backend_id']
                node.save()

    for node in cluster.node_set.exclude(backend_id=''):
        backend.pull_node(node)
        node.refresh_from_db()


@shared_task(name='waldur_rancher.pull_all_clusters_nodes')
def pull_all_clusters_nodes():
    for cluster in models.Cluster.objects.exclude(backend_id=''):
        pull_cluster_nodes(cluster.id)
        utils.update_cluster_nodes_states(cluster.id)


class PollRuntimeStateNodeTask(core_tasks.Task):
    max_retries = 600
    default_retry_delay = 10

    @classmethod
    def get_description(cls, node, *args, **kwargs):
        node = core_utils.deserialize_instance(node)
        return 'Poll node "%s"' % node.name

    def execute(self, node):
        pull_cluster_nodes(node.cluster_id)
        node.refresh_from_db()

        if node.runtime_state == models.Node.RuntimeStates.ACTIVE:
            # We don't need to change the node state here as it will be done
            # in an executor.
            return
        elif (
            node.runtime_state
            in [
                models.Node.RuntimeStates.REGISTERING,
                models.Node.RuntimeStates.UNAVAILABLE,
            ]
            or not node.runtime_state
        ):
            self.retry()
        elif node.runtime_state:
            raise RuntimeStateException(
                '%s (PK: %s) runtime state become erred: %s'
                % (node.__class__.__name__, node.pk, node.runtime_state)
            )

        return node


@shared_task(name='waldur_rancher.notify_create_user')
def notify_create_user(id, password, url):
    user = models.RancherUser.objects.get(id=id)
    email = user.user.email

    context = {
        'rancher_url': url,
        'user': user,
        'password': password,
    }

    core_utils.broadcast_mail('rancher', 'notification_create_user', context, [email])


@shared_task(name='waldur_rancher.sync_users')
def sync_users():
    if settings.WALDUR_RANCHER['READ_ONLY_MODE']:
        return
    SyncUser.run()
