from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.utils import CreateResourceProcessor, \
    UpdateResourceProcessor, DeleteResourceProcessor, get_spl_url, copy_attributes
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import views as package_views
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack import views as openstack_views
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
            template = order_item.plan.scope
        except ObjectDoesNotExist:
            template = None
        except AttributeError:
            template = None

        if not isinstance(template, package_models.PackageTemplate):
            raise serializers.ValidationError('Plan has invalid scope. VPC package template is expected.')

        project = order_item.order.project

        project_url = reverse('project-detail', kwargs={'uuid': project.uuid})
        spl_url = get_spl_url(openstack_models.OpenStackServiceProjectLink, order_item)
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
            **copy_attributes(fields, order_item)
        )

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


class OpenStackCreateResourceProcessor(CreateResourceProcessor):
    """
    Abstract base class to adapt OpenStack resource provisioning endpoints to marketplace API.
    """

    def get_serializer_class(self):
        return self.get_viewset().serializer_class

    def get_viewset(self):
        raise NotImplementedError

    def get_fields(self):
        raise NotImplementedError

    def get_post_data(self):
        order_item = self.order_item
        return dict(
            service_project_link=get_spl_url(tenant_models.OpenStackTenantServiceProjectLink, order_item),
            **copy_attributes(self.get_fields(), order_item)
        )

    def get_scope_from_response(self, response):
        return self.get_viewset().queryset.model.objects.get(uuid=response.data['uuid'])


class InstanceCreateProcessor(OpenStackCreateResourceProcessor):
    def get_viewset(self):
        return tenant_views.InstanceViewSet

    def get_fields(self):
        return (
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


class InstanceDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return tenant_views.InstanceViewSet


class VolumeCreateProcessor(OpenStackCreateResourceProcessor):
    def get_viewset(self):
        return tenant_views.VolumeViewSet

    def get_fields(self):
        return (
            'name',
            'description',
            'image',
            'size',
        )


class VolumeDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return tenant_views.VolumeViewSet
