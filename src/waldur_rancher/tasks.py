from __future__ import unicode_literals

import logging
import six

from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core import tasks as core_tasks
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.common import utils as common_utils
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.views import InstanceViewSet

from . import models, exceptions

logger = logging.getLogger(__name__)


class CreateNodeTask(core_tasks.Task):
    def execute(self, instance, node, user_id):
        cluster = instance
        content_type = ContentType.objects.get_for_model(openstack_tenant_models.Instance)
        flavor = node['flavor']
        storage = node['storage']
        image = node['image']
        subnet = node['subnet']
        roles = node['roles']
        group = node['group']
        tenant_spl = node['tenant_service_project_link']
        user = auth.get_user_model().objects.get(pk=user_id)

        roles_command = []
        if 'controlplane' in roles:
            roles_command.append('--controlplane')

        if 'etcd' in roles:
            roles_command.append('--etcd')

        if 'worker' in roles:
            roles_command.append('--worker')
        node_command = cluster.node_command + ' ' + ' '.join(roles_command)

        post_data = {
            'name': cluster.name + '_rancher_node',
            'flavor': reverse('openstacktenant-flavor-detail', kwargs={'uuid': flavor}),
            'image': reverse('openstacktenant-image-detail', kwargs={'uuid': image}),
            'service_project_link': reverse('openstacktenant-spl-detail', kwargs={'pk': tenant_spl}),
            'system_volume_size': storage,
            'security_groups': [{'url': reverse('openstacktenant-sgp-detail', kwargs={'uuid': group})}],
            'internal_ips_set': [
                {
                    'subnet': reverse('openstacktenant-subnet-detail', kwargs={'uuid': subnet})
                }
            ],
            'user_data': settings.WALDUR_RANCHER['RANCHER_NODE_CLOUD_INIT_TEMPLATE'].format(command=node_command)
        }
        view = InstanceViewSet.as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)

        if response.status_code != status.HTTP_201_CREATED:
            six.reraise(exceptions.RancherException, response.data)

        instance_uuid = response.data['uuid']
        instance = openstack_tenant_models.Instance.objects.get(uuid=instance_uuid)
        models.Node.objects.create(
            cluster=cluster,
            object_id=instance.id,
            content_type=content_type,
            controlplane_role='controlplane' in node['roles'],
            etcd_role='etcd' in node['roles'],
            worker_role='worker' in node['roles'],
        )

        resource_imported.send(
            sender=instance.__class__,
            instance=instance,
        )

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Create nodes for k8s cluster "%s".' % instance
