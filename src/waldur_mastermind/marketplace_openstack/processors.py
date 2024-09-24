from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import processors, signals
from waldur_mastermind.marketplace.processors import (
    copy_attributes,
    get_order_post_data,
)
from waldur_mastermind.marketplace_openstack import views
from waldur_openstack import models as openstack_models
from waldur_openstack import views as openstack_views

from . import utils


class TenantCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = views.MarketplaceTenantViewSet
    fields = (
        "name",
        "description",
        "user_username",
        "user_password",
        "subnet_cidr",
        "skip_connection_extnet",
        "availability_zone",
    )

    def get_post_data(self):
        order = self.order
        payload = get_order_post_data(order, self.get_fields())
        quotas = utils.map_limits_to_quotas(order.limits, order.offering)

        return dict(quotas=quotas, **payload)

    @classmethod
    def get_resource_model(cls):
        return openstack_models.Tenant


class TenantUpdateProcessor(processors.UpdateScopedResourceProcessor):
    def update_limits_process(self, user):
        scope = self.get_resource()
        if not scope or not isinstance(scope, openstack_models.Tenant):
            signals.resource_limit_update_failed.send(
                sender=self.order.resource.__class__,
                order=self.order,
                message=_("Limit updating is available only for tenants."),
            )
            return

        utils.update_limits(self.order)
        return True

    def send_request(self, user):
        return True


class TenantDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = openstack_views.TenantViewSet


class TenantMixin:
    def get_post_data(self):
        if not self.order.offering.scope:
            raise serializers.ValidationError(
                "Offering is invalid: it does not have a scope."
            )
        project_url = reverse(
            "project-detail", kwargs={"uuid": self.order.project.uuid}
        )
        tenant_url = reverse(
            "openstack-tenant-detail", kwargs={"uuid": self.order.offering.scope.uuid}
        )
        return dict(
            tenant=tenant_url,
            project=project_url,
            **copy_attributes(self.fields, self.order),
        )


class InstanceCreateProcessor(TenantMixin, processors.BaseCreateResourceProcessor):
    viewset = openstack_views.MarketplaceInstanceViewSet

    fields = (
        "name",
        "description",
        "flavor",
        "image",
        "security_groups",
        "server_group",
        "ports",
        "floating_ips",
        "system_volume_size",
        "system_volume_type",
        "data_volume_size",
        "data_volume_type",
        "volumes",
        "ssh_public_key",
        "user_data",
        "availability_zone",
        "connect_directly_to_external_network",
    )


class InstanceDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = openstack_views.MarketplaceInstanceViewSet


class VolumeCreateProcessor(TenantMixin, processors.BaseCreateResourceProcessor):
    viewset = openstack_views.MarketplaceVolumeViewSet

    fields = (
        "name",
        "description",
        "image",
        "size",
        "availability_zone",
        "type",
    )


class VolumeDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = openstack_views.MarketplaceVolumeViewSet
