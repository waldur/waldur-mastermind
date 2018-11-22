from django.core.exceptions import ObjectDoesNotExist
from rest_framework.reverse import reverse
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.utils import InternalOrderItemProcessor
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import views as package_views
from waldur_openstack.openstack import models as openstack_models


class OrderItemProcessor(InternalOrderItemProcessor):
    def get_serializer_class(self):
        return package_views.OpenStackPackageViewSet.create_serializer_class

    def get_viewset(self):
        return package_views.OpenStackPackageViewSet

    def get_post_data(self):
        return get_post_data(self.order_item)

    def get_scope_from_response(self, response):
        return package_models.OpenStackPackage.objects.get(uuid=response.data['uuid']).tenant


def get_post_data(order_item):
    try:
        service_settings = order_item.offering.scope
    except ObjectDoesNotExist:
        service_settings = None

    if not isinstance(service_settings, structure_models.ServiceSettings):
        raise serializers.ValidationError('Offering has invalid scope. Service settings object is expected.')

    try:
        template = order_item.plan.scope
    except ObjectDoesNotExist:
        template = None

    if not isinstance(template, package_models.PackageTemplate):
        raise serializers.ValidationError('Plan has invalid scope. VPC package template is expected.')

    project = order_item.order.project

    try:
        spl = openstack_models.OpenStackServiceProjectLink.objects.get(
            project=project,
            service__settings=service_settings,
            service__customer=project.customer,
        )
    except openstack_models.OpenStackServiceProjectLink.DoesNotExist:
        raise serializers.ValidationError('Project does not have access to the OpenStack service.')

    project_url = reverse('project-detail', kwargs={'uuid': project.uuid})
    spl_url = reverse('openstack-spl-detail', kwargs={'pk': spl.pk})
    template_url = reverse('package-template-detail', kwargs={'uuid': template.uuid})

    payload = dict(
        project=project_url,
        service_project_link=spl_url,
        template=template_url,
    )
    fields = (
        'name',
        'description',
        'user_username',
        'user_password',
        'subnet_cidr',
        'skip_connection_extnet',
        'availability_zone',
    )
    for field in fields:
        if field in order_item.attributes:
            payload[field] = order_item.attributes.get(field)

    return payload
