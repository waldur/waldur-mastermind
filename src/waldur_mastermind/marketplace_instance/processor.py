from django.core.exceptions import ObjectDoesNotExist
from rest_framework.reverse import reverse
from rest_framework import serializers, status

from waldur_core.structure import models as structure_models
from waldur_mastermind.common.utils import internal_api_request
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack_tenant import models as tenant_models
from waldur_openstack.openstack_tenant import views as tenant_views


def process_order_item(order_item, user):
    view = tenant_views.InstanceViewSet.as_view({'post': 'create'})
    post_data = get_post_data(order_item)
    response = internal_api_request(view, user, post_data)
    if response.status_code != status.HTTP_201_CREATED:
        raise serializers.ValidationError(response.data)

    order_item.scope = tenant_models.Instance.objects.get(uuid=response.data['uuid'])
    order_item.save()


def validate_order_item(order_item, request):
    post_data = get_post_data(order_item)
    serializer = tenant_views.InstanceViewSet.serializer_class(
        data=post_data, context={'request': request})
    serializer.is_valid(raise_exception=True)


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
