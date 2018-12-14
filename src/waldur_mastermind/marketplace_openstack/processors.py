from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.utils import CreateResourceProcessor, UpdateResourceProcessor
from waldur_mastermind.marketplace.utils import DeleteResourceProcessor
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import views as package_views
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack import views as openstack_views
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack_tenant import models as tenant_models
from waldur_openstack.openstack_tenant import views as tenant_views


class PackageCreateProcessor(CreateResourceProcessor):
    def get_serializer_class(self):
        return package_views.OpenStackPackageViewSet.create_serializer_class

    def get_viewset(self):
        return package_views.OpenStackPackageViewSet

    def get_post_data(self):
        order_item = self.order_item

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

    def get_scope_from_response(self, response):
        return package_models.OpenStackPackage.objects.get(uuid=response.data['uuid']).tenant


class PackageUpdateProcessor(UpdateResourceProcessor):

    def get_serializer_class(self):
        return package_views.OpenStackPackageViewSet.change_serializer_class

    def get_view(self):
        return package_views.OpenStackPackageViewSet.as_view({'post': 'change'})

    def get_post_data(self):
        resource = self.get_resource()
        try:
            package = package_models.OpenStackPackage.objects.get(tenant=resource)
        except ObjectDoesNotExist:
            raise serializers.ValidationError('OpenStack package for tenant does not exist.')

        template = self.order_item.plan.scope

        return {
            'package': reverse('openstack-package-detail', kwargs={'uuid': package.uuid}),
            'template': reverse('package-template-detail', kwargs={'uuid': template.uuid})
        }


class PackageDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return openstack_views.TenantViewSet


def get_spl(order_item):
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
    return spl


class InstanceCreateProcessor(CreateResourceProcessor):
    def get_serializer_class(self):
        return tenant_views.InstanceViewSet.serializer_class

    def get_viewset(self):
        return tenant_views.InstanceViewSet

    def get_post_data(self):
        order_item = self.order_item
        spl = get_spl(order_item)
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

    def get_scope_from_response(self, response):
        return tenant_models.Instance.objects.get(uuid=response.data['uuid'])


class InstanceDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return tenant_views.InstanceViewSet


class VolumeCreateProcessor(CreateResourceProcessor):
    def get_serializer_class(self):
        return tenant_views.VolumeViewSet.serializer_class

    def get_viewset(self):
        return tenant_views.VolumeViewSet

    def get_post_data(self):
        order_item = self.order_item
        spl = get_spl(order_item)

        payload = dict(
            service_project_link=reverse('openstacktenant-spl-detail', kwargs={'pk': spl.pk}),
        )

        fields = (
            'name',
            'description',
            'image',
            'size',
        )
        for field in fields:
            if field in order_item.attributes:
                payload[field] = order_item.attributes.get(field)
        return payload

    def get_scope_from_response(self, response):
        return tenant_models.Volume.objects.get(uuid=response.data['uuid'])


class VolumeDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return tenant_views.VolumeViewSet
