import logging
from typing import cast

from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models, processors, signals
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.processors import (
    copy_attributes,
    get_order_post_data,
)
from waldur_mastermind.marketplace_openstack import views
from waldur_mastermind.marketplace_openstack.utils import delete_instance
from waldur_openstack import models as openstack_models
from waldur_openstack import views as openstack_views
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError

from . import utils

logger = logging.getLogger(__name__)


class TenantCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = views.MarketplaceTenantViewSet
    fields = (
        "name",
        "description",
        "user_username",
        "user_password",
        "subnet_cidr",
        "skip_connection_extnet",
        "skip_creation_of_default_router",
        "skip_creation_of_default_subnet",
        "availability_zone",
        "security_groups",
    )

    def get_post_data(self):
        order = self.order
        payload = get_order_post_data(order, self.get_fields())
        # check for default override
        mtu = order.offering.plugin_options.get("default_internal_network_mtu")
        if mtu:
            # validate if MTU is a valid range and pass it as a default for the first network in a tenant
            try:
                value = int(mtu)
                if 68 <= value <= 9000:
                    payload["mtu"] = value
            except ValueError:
                logger.warning(
                    f"Invalid MTU value: {mtu} in {order.offering}. Skipping."
                )

        if order.limits:
            quotas = utils.map_limits_to_quotas(order.limits, order.offering)
            return dict(quotas=quotas, **payload)

        # Usage-based offerings don't require limits — tenants are created
        # with default quotas and billed by actual consumption.
        has_usage_billing = order.offering.components.filter(
            billing_type=BillingTypes.USAGE,
        ).exists()
        if has_usage_billing:
            return payload

        raise serializers.ValidationError(
            _("Order does not contain limits. Quotas are required to create a tenant.")
        )

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
    viewset = views.MarketplaceTenantViewSet


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
        "config_drive",
        "data_volumes",
        "metadata",
    )

    def validate_order(self, request):
        super().validate_order(request)
        self._run_pre_flight_check()

    def _run_pre_flight_check(self):
        """Pre-flight allocation check against OpenStack Placement.

        When the offering opts in via ``plugin_options.pre_flight_check_enabled``,
        ask Placement whether any compute host can currently satisfy the flavor
        of the requested instance and reject the order upfront if none can,
        instead of failing in the middle of provisioning.

        Only VCPU and MEMORY_MB are checked: Waldur instances boot from Cinder
        volumes, so the compute host's DISK_GB is not consumed. Querying DISK_GB
        would ask Placement for compute-node disk that boot-from-volume instances
        never allocate, falsely rejecting legitimate orders.

        Placement being missing or unreachable is treated as an infrastructure
        issue: log a warning and let the order proceed rather than blocking it.
        """
        offering = self.order.offering
        if not offering.plugin_options.get("pre_flight_check_enabled"):
            return

        tenant = offering.scope
        if tenant is None:
            return

        flavor_url = self.order.attributes.get("flavor")
        if not flavor_url:
            return
        try:
            flavor = core_utils.instance_from_url(flavor_url)
        except (ObjectDoesNotExist, Http404):
            # Invalid flavor is already reported by serializer validation.
            return

        settings = tenant.service_settings

        resources = {"VCPU": flavor.cores, "MEMORY_MB": flavor.ram}

        backend = OpenStackBackend(settings)
        try:
            raw = backend.get_allocation_candidates(resources=resources)
        except OpenStackBackendError as e:
            logger.warning(
                "Pre-flight allocation check skipped for order %s: "
                "Placement is unavailable (%s).",
                self.order.uuid,
                e,
            )
            return

        candidate_count = len(raw.get("allocation_requests", []))
        if candidate_count == 0:
            raise serializers.ValidationError(
                {
                    "flavor": _(
                        "No schedulable host: the cloud cannot currently "
                        "satisfy a request for %(cores)s vCPU / %(ram)s MB RAM. "
                        "Please choose a smaller flavor or try again later."
                    )
                    % {"cores": flavor.cores, "ram": flavor.ram}
                }
            )


class InstanceDeleteProcessor(processors.AbstractDeleteResourceProcessor):
    def validate_order(self, request):
        # Instance deletion preparation (stop, backups, snapshots) is handled
        # asynchronously by InstanceDeleteExecutor.
        pass

    def send_request(self, user, resource: models.Resource):
        if not resource.scope:
            return True
        delete_instance(resource.scope, self.order.attributes)
        return False


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


class VolumeDeleteProcessor(processors.AbstractDeleteResourceProcessor):
    """
    Volume delete processor that bypasses viewset permission filtering.

    This processor directly calls delete_volume utility function instead of
    making an internal API request through the viewset. This avoids permission
    filtering issues where users with marketplace termination permissions
    could not delete volumes due to GenericRoleFilter in the viewset.
    """

    def validate_order(self, request):
        volume = cast(openstack_models.Volume, self.order.resource.scope)
        if not volume:
            return
        # Validation is done in delete_volume function

    def send_request(self, user, resource: models.Resource):
        if not resource.scope:
            return True
        utils.delete_volume(resource.scope, self.order.attributes)
        return False
