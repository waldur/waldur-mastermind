from __future__ import unicode_literals

import logging

from celery import shared_task
from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core import tasks as core_tasks, utils as core_utils
from waldur_core.core.exceptions import RuntimeStateException
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.common import utils as common_utils
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.views import InstanceViewSet
from waldur_rancher.utils import SyncUser

from . import models, exceptions, utils, views

logger = logging.getLogger(__name__)


class CreateNodeTask(core_tasks.Task):
    def execute(self, instance, user_id):
        node = instance
        content_type = ContentType.objects.get_for_model(openstack_tenant_models.Instance)
        flavor = node.initial_data['flavor']
        system_volume_size = node.initial_data['system_volume_size']
        system_volume_type = node.initial_data.get('system_volume_type')
        data_volumes = node.initial_data.get('data_volumes', [])
        image = node.initial_data['image']
        subnet = node.initial_data['subnet']
        group = node.initial_data['group']
        tenant_spl = node.initial_data['tenant_service_project_link']
        user = auth.get_user_model().objects.get(pk=user_id)

        post_data = {
            'name': node.name,
            'flavor': reverse('openstacktenant-flavor-detail', kwargs={'uuid': flavor}),
            'image': reverse('openstacktenant-image-detail', kwargs={'uuid': image}),
            'service_project_link': reverse('openstacktenant-spl-detail', kwargs={'pk': tenant_spl}),
            'system_volume_size': system_volume_size,
            'system_volume_type': system_volume_type and reverse('openstacktenant-volume-type-detail', kwargs={'uuid': system_volume_type}),
            'data_volumes': [{
                'size': volume['size'],
                'volume_type': volume.get('volume_type') and reverse('openstacktenant-volume-type-detail', kwargs={'uuid': volume.get('volume_type')}),
            } for volume in data_volumes],
            'security_groups': [{'url': reverse('openstacktenant-sgp-detail', kwargs={'uuid': group})}],
            'internal_ips_set': [
                {
                    'subnet': reverse('openstacktenant-subnet-detail', kwargs={'uuid': subnet})
                }
            ],
            'user_data': utils.format_node_cloud_config(node),
        }
        view = InstanceViewSet.as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)

        if response.status_code != status.HTTP_201_CREATED:
            raise exceptions.RancherException(response.data)

        instance_uuid = response.data['uuid']
        instance = openstack_tenant_models.Instance.objects.get(uuid=instance_uuid)
        node.content_type = content_type
        node.object_id = instance.id

        # Set state here, because this task can be called from ClusterCreateExecutor and NodeCreateExecutor
        node.state = models.Node.States.CREATING

        node.save()

        resource_imported.send(
            sender=instance.__class__,
            instance=instance,
        )

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Create nodes for k8s cluster "%s".' % instance


class DeleteNodeTask(core_tasks.Task):
    def execute(self, instance, user_id):
        node = instance
        user = auth.get_user_model().objects.get(pk=user_id)
        view = InstanceViewSet.as_view({'delete': 'destroy'})
        response = common_utils.delete_request(view, user, uuid=node.instance.uuid.hex)

        if response.status_code != status.HTTP_202_ACCEPTED:
            raise exceptions.RancherException(response.data)


@shared_task
def update_nodes(cluster_id):
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
        backend.update_node_details(node)


@shared_task(name='waldur_rancher.update_clusters_nodes')
def update_clusters_nodes():
    for cluster in models.Cluster.objects.exclude(backend_id=''):
        update_nodes.delay(cluster.id)
        utils.update_cluster_nodes_states(cluster.id)


class PollRuntimeStateNodeTask(core_tasks.Task):
    max_retries = 1200
    default_retry_delay = 30

    @classmethod
    def get_description(cls, node, *args, **kwargs):
        node = core_utils.deserialize_instance(node)
        return 'Poll node "%s"' % node.name

    def execute(self, node):
        update_nodes(node.cluster_id)
        node.refresh_from_db()

        if node.runtime_state == models.Node.RuntimeStates.ACTIVE:
            return
        elif node.runtime_state == models.Node.RuntimeStates.REGISTERING or not node.runtime_state:
            self.retry()
        elif node.runtime_state:
            raise RuntimeStateException(
                '%s (PK: %s) runtime state become erred: %s' % (
                    node.__class__.__name__, node.pk, 'error'))

        return


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
    SyncUser.run()


class DeleteClusterNodesTask(core_tasks.Task):
    def execute(self, instance, user_id):
        cluster = instance
        view = views.NodeViewSet.as_view({'delete': 'destroy'})
        user = auth.get_user_model().objects.get(pk=user_id)

        for node in cluster.node_set.all():
            response = common_utils.delete_request(view, user, uuid=node.uuid.hex)

            if response.status_code != status.HTTP_202_ACCEPTED:
                node.error_message = 'Instance deleting\'s an error: %s.' % response.data
                node.set_erred()
                node.save()

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Delete nodes for k8s cluster "%s".' % instance


class RequestNodeCreation(core_tasks.Task):
    def execute(self, instance, user_id):
        cluster = instance
        user = auth.get_user_model().objects.get(pk=user_id)
        view = views.NodeViewSet.as_view({'post': 'create'})

        for post_data in cluster.initial_data['nodes']:
            response = common_utils.create_request(view, user, post_data)

            if response.status_code != status.HTTP_201_CREATED:
                raise exceptions.RancherException(response.data)

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Delete nodes for k8s cluster "%s".' % instance


class RetryNodeTask(core_tasks.Task):
    def execute(self, instance):
        node = instance
        post_data = node.initial_data.get('rest_initial_data')
        user_id = node.initial_data.get('rest_user_id')

        if not post_data or not user_id:
            raise exceptions.RancherException('Re-creating the node is not possible.')

        view = views.NodeViewSet.as_view({'post': 'create'})
        user = auth.get_user_model().objects.get(pk=user_id)

        with transaction.atomic():
            node.delete()
            response = common_utils.create_request(view, user, post_data=post_data)

            if response.status_code != status.HTTP_201_CREATED:
                raise exceptions.RancherException('Node recreating is fail: %s.' % response.data)

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Retry create node for k8s cluster "%s".' % instance.cluster
