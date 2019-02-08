from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import processors
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import views as package_views
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack import views as openstack_views
from waldur_openstack.openstack_tenant import views as tenant_views


class PackageCreateProcessor(processors.CreateResourceProcessor):
    def get_serializer_class(self):
        return package_views.OpenStackPackageViewSet.create_serializer_class

    def get_viewset(self):
        return package_views.OpenStackPackageViewSet

    def get_post_data(self):
        order_item = self.order_item

        try:
            template = order_item.plan.scope
        except ObjectDoesNotExist:
            template = None
        except AttributeError:
            template = None

        if not isinstance(template, package_models.PackageTemplate):
            raise serializers.ValidationError('Plan has invalid scope. VPC package template is expected.')

        project = order_item.order.project

        project_url = reverse('project-detail', kwargs={'uuid': project.uuid})
        spl_url = processors.get_spl_url(openstack_models.OpenStackServiceProjectLink, order_item)
        template_url = reverse('package-template-detail', kwargs={'uuid': template.uuid})

        fields = (
            'name',
            'description',
            'user_username',
            'user_password',
            'subnet_cidr',
            'skip_connection_extnet',
            'availability_zone',
        )

        return dict(
            project=project_url,
            service_project_link=spl_url,
            template=template_url,
            **processors.copy_attributes(fields, order_item)
        )

    def get_scope_from_response(self, response):
        return package_models.OpenStackPackage.objects.get(uuid=response.data['uuid']).tenant


class PackageUpdateProcessor(processors.UpdateResourceProcessor):

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


class PackageDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = openstack_views.TenantViewSet


class InstanceCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = tenant_views.InstanceViewSet

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


class InstanceDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = tenant_views.InstanceViewSet


class VolumeCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = tenant_views.VolumeViewSet

    fields = (
        'name',
        'description',
        'image',
        'size',
    )


class VolumeDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = tenant_views.VolumeViewSet
