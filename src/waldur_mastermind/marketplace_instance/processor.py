from django.core.exceptions import ObjectDoesNotExist
from rest_framework.reverse import reverse
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.utils import InternalOrderItemProcessor
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack_tenant import models as tenant_models
from waldur_openstack.openstack_tenant import views as tenant_views


class OrderItemProcessor(InternalOrderItemProcessor):
    def get_serializer_class(self):
        return tenant_views.InstanceViewSet.serializer_class

    def get_viewset(self):
        return tenant_views.InstanceViewSet

    def get_post_data(self):
        return get_post_data(self.order_item)

    def get_scope_from_response(self, response):
        return tenant_models.Instance.objects.get(uuid=response.data['uuid'])


def get_post_data(order_item):
    service_settings = order_item.offering.scope

    if not isinstance(service_settings, structure_models.ServiceSettings) or \
            service_settings.type != openstack_tenant_apps.OpenStackTenantConfig.service_name:
        raise serializers.ValidationError('Offering has invalid scope. Service settings object is expected.')

    project = order_item.order.project

    try:
        spl = tenant_models.OpenStackTenantServiceProjectLink.objects.get(
            project=project,
            service__settings=service_settings,
            service__customer=project.customer,
        )
    except ObjectDoesNotExist:
        raise serializers.ValidationError('Project does not have access to the OpenStack service.')

    payload = dict(
        service_project_link=reverse('openstacktenant-spl-detail', kwargs={'pk': spl.pk}),
    )

    fields = (
        'name',
        'description',
        'flavor',
        'image',
        'security_groups',
        'internal_ips_set',
        'floating_ips',
        'system_volume_size',
        'data_volume_size',
        'volumes',
        'ssh_public_key',
        'user_data',
    )
    for field in fields:
        if field in order_item.attributes:
            payload[field] = order_item.attributes.get(field)
    return payload
