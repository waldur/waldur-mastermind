from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import processors, signals
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import views as package_views
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack import views as openstack_views
from waldur_openstack.openstack_tenant import views as tenant_views

from . import utils


class TenantCreateProcessor(processors.CreateResourceProcessor):
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

        project_url = reverse('project-detail', kwargs={'uuid': project.uuid.hex})
        spl_url = processors.get_spl_url(openstack_models.OpenStackServiceProjectLink, order_item)

        fields = (
            'name',
            'description',
            'user_username',
            'user_password',
            'subnet_cidr',
            'skip_connection_extnet',
            'availability_zone',
        )

        quotas = utils.map_limits_to_quotas(order_item.limits)

        return dict(
            project=project_url,
            service_project_link=spl_url,
            template=template.uuid.hex,
            quotas=quotas,
            **processors.copy_attributes(fields, order_item)
        )

    def get_scope_from_response(self, response):
        return package_models.OpenStackPackage.objects.get(uuid=response.data['uuid']).tenant


class TenantUpdateProcessor(processors.UpdateResourceProcessor):

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
            'package': package.uuid.hex,
            'template': template.uuid.hex,
        }

    def update_limits_process(self, user):
        scope = self.order_item.resource.scope
        if not scope or not isinstance(scope, openstack_models.Tenant):
            signals.limit_update_failed.send(
                sender=self.order_item.resource.__class__,
                order_item=self.order_item,
                message='Limit updating is available only for tenants.'
            )
            return

        utils.update_limits(self.order_item)


class TenantDeleteProcessor(processors.DeleteResourceProcessor):
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
        'system_volume_type',
        'data_volume_size',
        'data_volume_type',
        'volumes',
        'ssh_public_key',
        'user_data',
        'availability_zone',
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
        'availability_zone',
        'type',
    )


class VolumeDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = tenant_views.VolumeViewSet
