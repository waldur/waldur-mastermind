import functools
import ipaddress
import logging
import re
import uuid
from datetime import UTC
from urllib.parse import urlparse, urlunparse

import httpx
from cinderclient import exceptions as cinder_exceptions
from cinderclient.v2.contrib import list_extensions
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import dateparse, timezone
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from glanceclient import exc as glance_exceptions
from keystoneauth1.exceptions.http import NotFound
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions
from requests import ConnectionError

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import create_batch_fetcher, pwgen
from waldur_core.logging import event_logger
from waldur_core.logging.diff import compute_collection_diff
from waldur_core.logging.enums import EventType
from waldur_core.structure.backend import ServiceBackend, log_backend_action
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.registry import get_resource_type
from waldur_core.structure.signals import resource_pulled
from waldur_core.structure.utils import (
    handle_resource_not_found,
    handle_resource_update_success,
    update_pulled_fields,
)
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
)
from waldur_openstack.exceptions import (
    OpenStackAuthorizationFailed,
    OpenStackBackendError,
    OpenStackTenantNotFound,
)
from waldur_openstack.octavia import get_octavia_client
from waldur_openstack.session import (
    get_cinder_client,
    get_glance_client,
    get_keystone_client,
    get_keystone_session,
    get_neutron_client,
    get_nova_client,
    get_placement_client,
    get_verify_ssl,
)
from waldur_openstack.utils import get_external_network_id, is_valid_volume_type_name

from . import audit, models, signals

logger = logging.getLogger(__name__)

VALID_ROUTER_INTERFACE_OWNERS = (
    "network:router_interface",
    "network:router_interface_distributed",
    "network:ha_router_replicated_interface",
)


def parse_comma_separated_list(value):
    return [field.strip() for field in value.split(",")]


def get_tenant_session(tenant: models.Tenant):
    return get_keystone_session(tenant.service_settings, tenant)


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except (
            neutron_exceptions.NeutronException,
            cinder_exceptions.ClientException,
            nova_exceptions.ClientException,
            glance_exceptions.BaseException,
        ) as e:
            # args is an empty list if no positional arguments were passed to the method
            # The first positional argument (args[0]) should be a Waldur model instance that is being operated on
            instance = args[0] if args else None

            if instance is not None and isinstance(
                instance, core_models.ErrorMessageMixin
            ):
                instance.error_message = str(e)
                instance.save(update_fields=["error_message"])
            else:
                logger.info("Exception in %s: %s", func.__name__, e)

            raise OpenStackBackendError(e)

    return wrapped


class OpenStackBackend(ServiceBackend):
    DEFAULTS = {
        "tenant_name": "admin",
        "console_type": "novnc",
        "verify_ssl": False,
    }

    def __init__(self, settings):
        self.settings: ServiceSettings = settings

    @property
    def admin_session(self):
        return get_keystone_session(self.settings)

    def ping(self, raise_exception=False):
        try:
            get_keystone_client(self.admin_session)
        except keystone_exceptions.ClientException as e:
            if raise_exception:
                raise OpenStackBackendError(e)
            return False
        else:
            return True

    def ping_resource(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.get(instance.backend_id)
        except (ConnectionError, nova_exceptions.ClientException):
            return False
        else:
            return True

    def validate_settings(self):
        if not self.check_admin_tenant():
            raise ValidationError(_("Provided credentials are not for admin tenant."))

    def check_admin_tenant(self):
        try:
            get_keystone_client(self.admin_session)
        except keystone_exceptions.AuthorizationFailure:
            return False
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return True

    def get_tenant_limits(self, tenant: models.Tenant, fixed=True):
        tenant_backend_id = tenant.backend_id
        session = self.admin_session

        nova = get_nova_client(session)
        cinder = get_cinder_client(session)

        try:
            nova_quotas = nova.quotas.get(tenant_id=tenant_backend_id)
            cinder_quotas = cinder.quotas.get(tenant_id=tenant_backend_id)
        except (
            nova_exceptions.ClientException,
            cinder_exceptions.ClientException,
        ) as e:
            raise OpenStackBackendError(e)

        limits = {
            RAM_TYPE: nova_quotas.ram,
            CORES_TYPE: nova_quotas.cores,
        }

        if fixed:
            limits[STORAGE_TYPE] = self.gb2mb(cinder_quotas.gigabytes)
        else:
            for name, value in cinder_quotas._info.items():
                if is_valid_volume_type_name(name):
                    limits[name] = value

        return limits

    def get_tenant_quotas_limits(self, tenant: models.Tenant):
        tenant_backend_id = tenant.backend_id
        session = get_tenant_session(tenant)

        nova = get_nova_client(session)
        neutron = get_neutron_client(session)
        cinder = get_cinder_client(session)

        try:
            nova_quotas = nova.quotas.get(tenant_id=tenant_backend_id)
            cinder_quotas = cinder.quotas.get(tenant_id=tenant_backend_id)
            neutron_quotas = neutron.show_quota(tenant_id=tenant_backend_id)["quota"]
        except (
            nova_exceptions.ClientException,
            cinder_exceptions.ClientException,
            neutron_exceptions.NeutronClientException,
        ) as e:
            raise OpenStackBackendError(e)

        quotas = {
            "ram": nova_quotas.ram,
            "vcpu": nova_quotas.cores,
            "storage": self.gb2mb(cinder_quotas.gigabytes),
            "snapshots": cinder_quotas.snapshots,
            "volumes": cinder_quotas.volumes,
            "instances": nova_quotas.instances,
            "security_group_count": neutron_quotas["security_group"],
            "security_group_rule_count": neutron_quotas["security_group_rule"],
            "floating_ip_count": neutron_quotas["floatingip"],
            "port_count": neutron_quotas["port"],
            "network_count": neutron_quotas["network"],
            "subnet_count": neutron_quotas["subnet"],
        }

        for name, value in cinder_quotas._info.items():
            if is_valid_volume_type_name(name):
                quotas[name] = value

        return quotas

    def get_tenant_quotas_usage(self, tenant: models.Tenant):
        tenant_backend_id = tenant.backend_id
        session = get_tenant_session(tenant)

        nova = get_nova_client(session)
        neutron = get_neutron_client(session)
        cinder = get_cinder_client(session)

        try:
            nova_quotas = nova.quotas.get(
                tenant_id=tenant_backend_id, detail=True
            )._info
            neutron_quotas = neutron.show_quota_details(tenant_backend_id)["quota"]
            # There are no cinder quotas for total volumes and snapshots size.
            # Therefore we need to compute them manually by fetching list of volumes and snapshots in the tenant.
            # Also `list` method in volume and snapshots does not implement filtering by tenant ID.
            # That's why we need to assume that tenant_id field is set up in backend settings.
            volumes = cinder.volumes.list()
            snapshots = cinder.volume_snapshots.list()
            cinder_quotas = cinder.quotas.get(
                tenant_id=tenant_backend_id, usage=True
            )._info
        except (
            nova_exceptions.ClientException,
            neutron_exceptions.NeutronClientException,
            cinder_exceptions.ClientException,
        ) as e:
            raise OpenStackBackendError(e)

        # Cinder quotas for volumes and snapshots size are not available in REST API
        # therefore we need to calculate them manually
        volumes_size = sum(self.gb2mb(v.size) for v in volumes)
        snapshots_size = sum(self.gb2mb(v.size) for v in snapshots)

        quotas = {
            # Nova quotas
            "ram": nova_quotas["ram"]["in_use"],
            "vcpu": nova_quotas["cores"]["in_use"],
            "instances": nova_quotas["instances"]["in_use"],
            # Neutron quotas
            "security_group_count": neutron_quotas["security_group"]["used"],
            "security_group_rule_count": neutron_quotas["security_group_rule"]["used"],
            "floating_ip_count": neutron_quotas["floatingip"]["used"],
            "port_count": neutron_quotas["port"]["used"],
            "network_count": neutron_quotas["network"]["used"],
            "subnet_count": neutron_quotas["subnet"]["used"],
            # Cinder quotas
            "storage": self.gb2mb(cinder_quotas["gigabytes"]["in_use"]),
            "volumes": len(volumes),
            "volumes_size": volumes_size,
            "snapshots": len(snapshots),
            "snapshots_size": snapshots_size,
        }

        for name, value in cinder_quotas.items():
            if is_valid_volume_type_name(name):
                quotas[name] = value["in_use"]

        return quotas

    def pull_service_properties(self):
        self.pull_service_settings_quotas()
        self.pull_global_volume_types()
        self.pull_global_flavors()
        self.pull_global_images()
        self.pull_external_networks()
        self.pull_hypervisors()

    def pull_resources(self):
        self.pull_tenants()

    def pull_tenants(self):
        keystone = get_keystone_client(self.admin_session)
        logger.info("Starting to pull tenants for service settings %s", self.settings)

        try:
            domain = self._get_domain()
            logger.debug("Using domain: %s (type: %s)", domain, type(domain).__name__)
            backend_tenants = keystone.projects.list(domain=domain)
        except keystone_exceptions.Forbidden as e:
            if "identity:list_projects" in str(e):
                logger.warning(
                    "User is not authorized to list all projects. This might be expected if the user only has access to specific projects. Error: %s",
                    str(e),
                )
                # Get only the projects the user has access to
                try:
                    backend_tenants = keystone.projects.list()
                    logger.info(
                        "Successfully retrieved accessible projects. Count: %d",
                        len(backend_tenants),
                    )
                except keystone_exceptions.ClientException as e2:
                    logger.error("Failed to list accessible projects: %s", str(e2))
                    raise OpenStackBackendError(e2)
            else:
                logger.error("Permission denied while listing projects: %s", str(e))
                raise OpenStackBackendError(e)
        except keystone_exceptions.ClientException as e:
            logger.error("Failed to list projects: %s", str(e))
            raise OpenStackBackendError(e)

        backend_tenants_mapping = {tenant.id: tenant for tenant in backend_tenants}
        logger.info("Retrieved %d tenants from backend", len(backend_tenants_mapping))

        tenants = models.Tenant.objects.filter(
            state__in=[CoreStates.OK, CoreStates.ERRED],
            service_settings=self.settings,
        )
        logger.info("Found %d tenants in database to sync", tenants.count())

        for tenant in tenants:
            backend_tenant = backend_tenants_mapping.get(tenant.backend_id)
            if backend_tenant is None:
                logger.warning(
                    "Tenant %s (backend_id: %s) not found in backend",
                    tenant.name,
                    tenant.backend_id,
                )
                handle_resource_not_found(tenant)
                signals.tenant_does_not_exist_in_backend.send(
                    models.Tenant, instance=tenant
                )
                continue

            logger.debug(
                "Updating tenant %s (backend_id: %s) from backend data",
                tenant.name,
                tenant.backend_id,
            )
            imported_backend_tenant = models.Tenant(
                name=backend_tenant.name,
                description=backend_tenant.description,
                backend_id=backend_tenant.id,
                state=CoreStates.OK,
            )
            update_pulled_fields(
                tenant, imported_backend_tenant, models.Tenant.get_backend_fields()
            )
            handle_resource_update_success(tenant)

    def _get_domain(self):
        """Get current domain"""
        keystone = get_keystone_client(self.admin_session)
        domain_name = self.settings.domain or "Default"
        logger.debug("Attempting to get domain with name: %s", domain_name)

        try:
            domain = keystone.domains.find(name=domain_name)
            logger.debug("Successfully found domain object for %s", domain_name)
            return domain
        except keystone_exceptions.Forbidden as e:
            if "identity:list_domains" in str(e):
                logger.warning(
                    "User is not authorized to list domains. Using domain name as string: %s. Error: %s",
                    domain_name,
                    str(e),
                )
                return domain_name
            logger.error("Permission denied while getting domain: %s", str(e))
            raise OpenStackBackendError(e)
        except keystone_exceptions.ClientException as e:
            logger.error("Failed to get domain: %s", str(e))
            raise OpenStackBackendError(e)

    def remove_ssh_key_from_tenant(
        self, tenant: models.Tenant, key_name, fingerprint_md5
    ):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)

        # There could be leftovers of key duplicates: remove them all
        keys = nova.keypairs.findall(fingerprint=fingerprint_md5)
        for key in keys:
            # Remove only keys created with Waldur
            if key.name == key_name:
                nova.keypairs.delete(key)

        logger.info("Deleted ssh public key %s from backend", key_name)

    def _are_rules_equal(self, backend_rule, local_rule):
        if backend_rule["ethertype"] != local_rule.ethertype:
            return False
        if backend_rule["direction"] != local_rule.direction:
            return False
        if backend_rule["port_range_min"] != local_rule.from_port:
            return False
        if backend_rule["port_range_max"] != local_rule.to_port:
            return False
        if backend_rule["protocol"] != local_rule.protocol:
            return False
        if backend_rule["remote_ip_prefix"] != local_rule.cidr:
            return False
        if backend_rule["remote_group_id"] != (
            local_rule.remote_group.backend_id if local_rule.remote_group else None
        ):
            return False
        if backend_rule["description"] != local_rule.description:
            return False
        return True

    def pull_tenant_images(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        glance = get_glance_client(session)
        try:
            remote_images = glance.images.list()
        except glance_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        remote_images = [
            image for image in remote_images if not image["status"] == "deleted"
        ]

        def get_backend_created_at(image):
            value = image.get("created_at")
            if not value:
                return None
            parsed = dateparse.parse_datetime(value)
            if parsed and timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone=UTC)
            return parsed

        local_image_mapping = self._tenant_mappings(tenant.images.all())
        local_image_ids = set(local_image_mapping.keys())

        remote_image_mapping = {image["id"]: image for image in remote_images}
        remote_image_ids = set(remote_image_mapping.keys())

        stale_image_ids = local_image_ids - remote_image_ids
        for image_backend_id in stale_image_ids:
            local_image = local_image_mapping[image_backend_id]
            tenant.images.remove(local_image)

        new_image_ids = remote_image_ids - local_image_ids
        for image_backend_id in new_image_ids:
            remote_image = remote_image_mapping[image_backend_id]
            local_image, _ = models.Image.all_objects.update_or_create(
                settings=self.settings,
                backend_id=remote_image["id"],
                defaults={
                    "name": remote_image["name"]
                    or remote_image.get("description")
                    or remote_image["id"],
                    "min_ram": remote_image["min_ram"],
                    "min_disk": self.gb2mb(remote_image["min_disk"]),
                    "backend_created_at": get_backend_created_at(remote_image),
                    # Glance v2 returns custom properties as flat top-level
                    # keys (verified empirically against the lab cloud).
                    "hw_rescue_device": remote_image.get("hw_rescue_device") or "",
                    "hw_rescue_bus": remote_image.get("hw_rescue_bus") or "",
                },
            )
            tenant.images.add(local_image)

        existing_image_ids = remote_image_ids & local_image_ids
        for image_backend_id in existing_image_ids:
            remote_image = remote_image_mapping[image_backend_id]
            local_image, _ = models.Image.all_objects.update_or_create(
                settings=self.settings,
                backend_id=remote_image["id"],
                defaults={
                    "name": remote_image["name"]
                    or remote_image.get("description")
                    or remote_image["id"],
                    "min_ram": remote_image["min_ram"],
                    "min_disk": self.gb2mb(remote_image["min_disk"]),
                    "backend_created_at": get_backend_created_at(remote_image),
                    "hw_rescue_device": remote_image.get("hw_rescue_device") or "",
                    "hw_rescue_bus": remote_image.get("hw_rescue_bus") or "",
                },
            )

    def pull_tenant_flavors(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)
        try:
            remote_flavors = nova.flavors.findall()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        flavor_exclude_regex = self.settings.options.get("flavor_exclude_regex", "")
        if flavor_exclude_regex:
            name_pattern = re.compile(flavor_exclude_regex)
            filtered_remote_flavors = filter(
                lambda flavor: name_pattern.match(flavor.name) is None, remote_flavors
            )
            skipped_flavors = set(
                flavor.id for flavor in filtered_remote_flavors
            ) - set(flavor.id for flavor in remote_flavors)
            if skipped_flavors:
                logger.debug(
                    "Skipping pull of %s flavors as they match %s regex pattern.",
                    ", ".join(skipped_flavors),
                    flavor_exclude_regex,
                )
            remote_flavors = filtered_remote_flavors

        local_flavor_mapping = self._tenant_mappings(tenant.flavors.all())
        local_flavor_ids = set(local_flavor_mapping.keys())

        remote_flavor_mapping = {flavor.id: flavor for flavor in remote_flavors}
        remote_flavor_ids = set(remote_flavor_mapping.keys())

        stale_flavor_ids = local_flavor_ids - remote_flavor_ids
        for flavor_backend_id in stale_flavor_ids:
            local_flavor = local_flavor_mapping[flavor_backend_id]
            tenant.flavors.remove(local_flavor)

        new_flavor_ids = remote_flavor_ids - local_flavor_ids
        for flavor_backend_id in new_flavor_ids:
            remote_flavor = remote_flavor_mapping[flavor_backend_id]
            local_flavor, _ = models.Flavor.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_flavor.id,
                defaults={
                    "name": remote_flavor.name,
                    "cores": remote_flavor.vcpus,
                    "ram": remote_flavor.ram,
                    "disk": self.gb2mb(remote_flavor.disk),
                },
            )
            tenant.flavors.add(local_flavor)

        existing_flavor_ids = remote_flavor_ids & local_flavor_ids
        for flavor_backend_id in existing_flavor_ids:
            remote_flavor = remote_flavor_mapping[flavor_backend_id]
            local_flavor, _ = models.Flavor.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_flavor.id,
                defaults={
                    "name": remote_flavor.name,
                    "cores": remote_flavor.vcpus,
                    "ram": remote_flavor.ram,
                    "disk": self.gb2mb(remote_flavor.disk),
                },
            )

    def pull_global_flavors(self):
        nova = get_nova_client(self.admin_session)
        try:
            remote_flavors = nova.flavors.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        models.Flavor.objects.filter(settings=self.settings).exclude(
            backend_id__in=[flavor.id for flavor in remote_flavors]
        ).delete()
        for remote_flavor in remote_flavors:
            models.Flavor.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_flavor.id,
                defaults={
                    "name": remote_flavor.name,
                    "cores": remote_flavor.vcpus,
                    "ram": remote_flavor.ram,
                    "disk": self.gb2mb(remote_flavor.disk),
                },
            )

    # Mapping from Placement resource_class → legacy Hypervisor field names.
    # Used to populate the backward-compatible vcpus / memory_mb / local_gb
    # columns on Hypervisor from the per-class HypervisorInventory rows. Other
    # classes (VGPU, PCI_DEVICE, NUMA_CORE, CUSTOM_*) are stored in
    # HypervisorInventory but have no legacy column.
    _LEGACY_HYPERVISOR_CAPACITY_FIELDS = {
        "VCPU": ("vcpus", "vcpus_used"),
        "MEMORY_MB": ("memory_mb", "memory_mb_used"),
        "DISK_GB": ("local_gb", "local_gb_used"),
    }

    @staticmethod
    def _effective_total(inv):
        """Capacity the Nova scheduler treats as available."""
        total = max(inv.get("total", 0) - inv.get("reserved", 0), 0)
        return int(total * (inv.get("allocation_ratio", 1.0) or 1.0))

    def _collect_placement_data(self):
        """Return per-resource-provider capacity and traits, keyed by RP name.

        Returns a ``(capacity, traits)`` tuple.

        ``capacity`` maps resource-provider name → per-class inventory::

            {
              "compute01": {
                "VCPU":      {"total": 72, "reserved": 0, "allocation_ratio": 16.0, "used": 5},
                "MEMORY_MB": {...},
                "DISK_GB":   {...},
                "VGPU":      {...},   # only when present on this RP
                ...
              },
              ...
            }

        Raw values are preserved (not pre-multiplied) so admins can answer
        "why is the effective vCPU count what it is?" from `HypervisorInventory`
        rows. Compute the effective total via `_effective_total(inv)` when
        needed.

        ``traits`` maps resource-provider name → list of trait names::

            {"compute01": ["HW_CPU_X86_AVX2", "STORAGE_DISK_SSD", "CUSTOM_GOLD"]}

        For compute-node resource providers, the provider name equals
        hypervisor_hostname, which is how we link Placement back to the Nova
        Hypervisor records below.
        """
        placement = get_placement_client(self.admin_session)
        rps = placement.list_resource_providers()
        capacity = {}
        traits = {}
        for rp in rps:
            rp_uuid = rp.get("uuid")
            if not rp_uuid:
                continue
            try:
                inventories = placement.get_inventories(rp_uuid)
                usages = placement.get_usages(rp_uuid)
                rp_traits = placement.get_traits(rp_uuid)
            except OpenStackBackendError as e:
                logger.warning(
                    "Skipping Placement resource provider %s due to error: %s",
                    rp.get("name"),
                    e,
                )
                continue
            per_class = {}
            for resource_class, inv in inventories.items():
                if not isinstance(inv, dict):
                    continue
                per_class[resource_class] = {
                    "total": int(inv.get("total", 0)),
                    "reserved": int(inv.get("reserved", 0)),
                    "allocation_ratio": float(inv.get("allocation_ratio", 1.0) or 1.0),
                    "used": int(usages.get(resource_class, 0)),
                }
            capacity[rp.get("name")] = per_class
            traits[rp.get("name")] = list(rp_traits)
        return capacity, traits

    def pull_hypervisors(self):
        nova = get_nova_client(self.admin_session)
        try:
            remote_hypervisors = nova.hypervisors.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        # Capacity now comes from Placement; Nova's hypervisor_stats fields are
        # documented as misleading (ignored CPU pinning, file-backed memory,
        # shared storage) and removed at microversion 2.88. running_vms, state
        # and status remain Nova-sourced for now (still present at 2.87).
        placement_capacity, placement_traits = self._collect_placement_data()

        remote_ids = [str(h.id) for h in remote_hypervisors]
        stale_qs = models.Hypervisor.objects.filter(settings=self.settings).exclude(
            backend_id__in=remote_ids
        )
        for stale in stale_qs:
            logger.info(
                "Deleting stale hypervisor %s (backend_id=%s) for settings %s.",
                stale.name,
                stale.backend_id,
                self.settings,
            )
        stale_qs.delete()

        for remote in remote_hypervisors:
            per_class = placement_capacity.get(remote.hypervisor_hostname, {})
            # Maintain the legacy three columns on Hypervisor for backward-
            # compat with the existing summary endpoint and the homeport chart.
            legacy_fields = {}
            for resource_class, (
                cap_field,
                used_field,
            ) in self._LEGACY_HYPERVISOR_CAPACITY_FIELDS.items():
                inv = per_class.get(resource_class)
                if inv is None:
                    legacy_fields[cap_field] = 0
                    legacy_fields[used_field] = 0
                else:
                    legacy_fields[cap_field] = self._effective_total(inv)
                    legacy_fields[used_field] = inv.get("used", 0)

            hypervisor, created = models.Hypervisor.objects.update_or_create(
                settings=self.settings,
                backend_id=str(remote.id),
                defaults={
                    "name": remote.hypervisor_hostname,
                    "hypervisor_type": getattr(remote, "hypervisor_type", ""),
                    "running_vms": getattr(remote, "running_vms", 0),
                    "state": getattr(remote, "state", ""),
                    "status": getattr(remote, "status", ""),
                    **legacy_fields,
                },
            )
            if created:
                logger.info(
                    "Created new hypervisor %s (backend_id=%s) for settings %s.",
                    remote.hypervisor_hostname,
                    remote.id,
                    self.settings,
                )

            # Mirror Placement's view: the per-class inventory becomes the
            # source of truth for capacity. Drop classes that the host no
            # longer reports (e.g. VGPU pool removed).
            seen_classes = set()
            for resource_class, inv in per_class.items():
                models.HypervisorInventory.objects.update_or_create(
                    hypervisor=hypervisor,
                    resource_class=resource_class,
                    defaults={
                        "total": inv["total"],
                        "reserved": inv["reserved"],
                        "allocation_ratio": inv["allocation_ratio"],
                        "used": inv["used"],
                    },
                )
                seen_classes.add(resource_class)
            hypervisor.inventories.exclude(resource_class__in=seen_classes).delete()

            # Sync Placement traits (capability flags) as an M2M. Trait rows
            # form a global catalog shared across hosts; .set() replaces the
            # host's links, so traits dropped from the resource provider are
            # detached on the next pull. Orphan Trait rows are left in place —
            # they are a harmless name catalog.
            trait_objs = [
                models.Trait.objects.get_or_create(name=name)[0]
                for name in placement_traits.get(remote.hypervisor_hostname, [])
            ]
            hypervisor.traits.set(trait_objs)

    def pull_global_images(self):
        glance = get_glance_client(self.admin_session)
        try:
            remote_images = list(glance.images.list())
        except glance_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        remote_images = [
            image for image in remote_images if image["status"] != "deleted"
        ]

        def get_backend_created_at(image):
            value = image.get("created_at")
            if not value:
                return None
            parsed = dateparse.parse_datetime(value)
            if parsed and timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone=UTC)
            return parsed

        # Only garbage-collect rows no tenant claims: tenant-private images
        # may be invisible to the admin session (restrictive Glance policy),
        # and a concurrent pull_tenant_images may have just linked a fresh
        # image created after this listing was snapshotted. Rows for
        # genuinely deleted images are first unlinked by pull_tenant_images
        # and removed here once orphaned.
        models.Image.all_objects.filter(
            settings=self.settings, tenants__isnull=True
        ).exclude(backend_id__in=[image["id"] for image in remote_images]).delete()
        for remote_image in remote_images:
            models.Image.all_objects.update_or_create(
                settings=self.settings,
                backend_id=remote_image["id"],
                defaults={
                    "name": remote_image["name"]
                    or remote_image.get("description")
                    or remote_image["id"],
                    "min_ram": remote_image["min_ram"],
                    "min_disk": self.gb2mb(remote_image["min_disk"]),
                    "backend_created_at": get_backend_created_at(remote_image),
                },
            )

    def pull_global_volume_types(self):
        cinder = get_cinder_client(self.admin_session)
        try:
            remote_volume_types = cinder.volume_types.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume_type_blacklist = parse_comma_separated_list(
            self.settings.options.get("volume_type_blacklist", "")
        )
        models.VolumeType.objects.filter(settings=self.settings).exclude(
            backend_id__in=[volume_type.id for volume_type in remote_volume_types]
        ).delete()
        for volume_type in remote_volume_types:
            models.VolumeType.objects.update_or_create(
                settings=self.settings,
                backend_id=volume_type.id,
                defaults={
                    "name": volume_type.name,
                    "description": volume_type.description or "",
                    "disabled": volume_type.name in volume_type_blacklist,
                },
            )

    def pull_external_networks(self):
        neutron = get_neutron_client(self.admin_session)
        try:
            remote_networks = neutron.list_networks(**{"router:external": True})[
                "networks"
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        remote_network_ids = [n["id"] for n in remote_networks]

        # Delete stale external networks
        models.ExternalNetwork.objects.filter(settings=self.settings).exclude(
            backend_id__in=remote_network_ids
        ).delete()

        for net in remote_networks:
            ext_net, _ = models.ExternalNetwork.objects.update_or_create(
                settings=self.settings,
                backend_id=net["id"],
                defaults={
                    "name": net.get("name", ""),
                    "is_shared": net.get("shared", False),
                    "is_default": net.get("is_default", False),
                    "status": net.get("status", ""),
                    "description": net.get("description", ""),
                },
            )

            # Sync subnets for this external network
            try:
                remote_subnets = neutron.list_subnets(network_id=net["id"])["subnets"]
            except neutron_exceptions.NeutronClientException as e:
                logger.warning(
                    "Failed to list subnets for external network %s: %s",
                    net["id"],
                    e,
                )
                continue

            remote_subnet_ids = [s["id"] for s in remote_subnets]
            models.ExternalSubnet.objects.filter(network=ext_net).exclude(
                backend_id__in=remote_subnet_ids
            ).delete()

            for subnet in remote_subnets:
                models.ExternalSubnet.objects.update_or_create(
                    network=ext_net,
                    backend_id=subnet["id"],
                    defaults={
                        "name": subnet.get("name", ""),
                        "cidr": subnet.get("cidr", ""),
                        "gateway_ip": subnet.get("gateway_ip"),
                        "ip_version": subnet.get("ip_version", 4),
                        "enable_dhcp": subnet.get("enable_dhcp", True),
                        "allocation_pools": subnet.get("allocation_pools", []),
                        "dns_nameservers": subnet.get("dns_nameservers", []),
                        "description": subnet.get("description", ""),
                    },
                )

    def pull_tenant_volume_types(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)
        try:
            remote_volume_types = cinder.volume_types.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        volume_type_blacklist = parse_comma_separated_list(
            tenant.service_settings.options.get("volume_type_blacklist", "")
        )

        local_volume_type_mapping = self._tenant_mappings(tenant.volume_types.all())
        local_volume_type_ids = set(local_volume_type_mapping.keys())

        remote_volume_type_mapping = {
            volume_type.id: volume_type for volume_type in remote_volume_types
        }
        remote_volume_type_ids = set(remote_volume_type_mapping.keys())

        stale_volume_type_ids = local_volume_type_ids - remote_volume_type_ids
        for volume_type_backend_id in stale_volume_type_ids:
            local_volume_type = local_volume_type_mapping[volume_type_backend_id]
            tenant.volume_types.remove(local_volume_type)

        new_volume_type_ids = remote_volume_type_ids - local_volume_type_ids
        for volume_type_backend_id in new_volume_type_ids:
            remote_volume_type = remote_volume_type_mapping[volume_type_backend_id]
            local_volume_type, _ = models.VolumeType.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_volume_type.id,
                defaults={
                    "name": remote_volume_type.name,
                    "description": remote_volume_type.description or "",
                    "disabled": remote_volume_type.name in volume_type_blacklist,
                },
            )
            tenant.volume_types.add(local_volume_type)

        existing_volume_type_ids = remote_volume_type_ids & local_volume_type_ids
        for volume_type_backend_id in existing_volume_type_ids:
            remote_volume_type = remote_volume_type_mapping[volume_type_backend_id]
            local_volume_type, _ = models.VolumeType.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_volume_type.id,
                defaults={
                    "name": remote_volume_type.name,
                    "description": remote_volume_type.description or "",
                    "disabled": remote_volume_type.name in volume_type_blacklist,
                },
            )

    @log_backend_action("push quotas for tenant")
    def push_tenant_quotas(self, tenant: models.Tenant, quotas: dict[str, int]):
        cinder_quotas = {
            "gigabytes": self.mb2gb(quotas.get("storage"))
            if "storage" in quotas
            else None,
            "volumes": quotas.get("volumes"),
            "snapshots": quotas.get("snapshots"),
        }

        cinder_quotas = {k: v for k, v in cinder_quotas.items() if v is not None}

        # Filter volume-type quotas.
        volume_type_quotas = dict(
            (key, value)
            for (key, value) in quotas.items()
            if is_valid_volume_type_name(key) and value is not None
        )

        if volume_type_quotas:
            cinder_quotas.update(volume_type_quotas)

        nova_quotas = {
            "instances": quotas.get("instances"),
            "cores": quotas.get("vcpu"),
            "ram": quotas.get("ram"),
        }
        nova_quotas = {k: v for k, v in nova_quotas.items() if v is not None}

        neutron_quotas = {
            "security_group": quotas.get("security_group_count"),
            "security_group_rule": quotas.get("security_group_rule_count"),
            "floatingip": quotas.get("floating_ip_count"),
            "network": quotas.get("network_count"),
            "subnet": quotas.get("subnet_count"),
            "port": quotas.get("port_count"),
        }
        neutron_quotas = {k: v for k, v in neutron_quotas.items() if v is not None}

        session = self.admin_session
        try:
            cinder = get_cinder_client(session)
            nova = get_nova_client(session)
            neutron = get_neutron_client(session)

            if cinder_quotas:
                cinder.quotas.update(tenant.backend_id, **cinder_quotas)
            if nova_quotas:
                nova.quotas.update(tenant.backend_id, **nova_quotas)
            if neutron_quotas:
                neutron.update_quota(tenant.backend_id, {"quota": neutron_quotas})
        except Exception as e:
            raise OpenStackBackendError(e)

    @log_backend_action("pull quotas for tenant")
    def pull_tenant_quotas(self, tenant: models.Tenant):
        for quota_name, limit in self.get_tenant_quotas_limits(tenant).items():
            tenant.set_quota_limit(quota_name, limit)
        for quota_name, usage in self.get_tenant_quotas_usage(tenant).items():
            tenant.set_quota_usage(quota_name, usage)
        # Notify downstream integrations that a fresh pull completed, even when
        # set_quota_usage produced no deltas. Without this, stable workloads
        # never refresh marketplace-side snapshots that key off QuotaUsage saves.
        signals.tenant_quotas_pulled.send(models.Tenant, instance=tenant)

    @log_backend_action("pull floating IPs for tenant")
    def pull_tenant_floating_ips(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            backend_floating_ips = neutron.list_floatingips(
                tenant_id=tenant.backend_id
            )["floatingips"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            self._update_tenant_floating_ips(tenant, backend_floating_ips)
            self._remove_stale_floating_ips([tenant], backend_floating_ips)

    def _remove_stale_floating_ips(self, tenants, backend_floating_ips):
        remote_ids = {ip["id"] for ip in backend_floating_ips}
        stale_ips = models.FloatingIP.objects.filter(
            tenant__in=tenants,
            state__in=[CoreStates.OK, CoreStates.ERRED],
        ).exclude(backend_id__in=remote_ids)
        stale_ips.delete()

    def _update_tenant_floating_ips(self, tenant: models.Tenant, backend_floating_ips):
        floating_ips: dict[str, models.FloatingIP] = {
            ip.backend_id: ip for ip in tenant.floating_ips.exclude(backend_id="")
        }

        for backend_ip in backend_floating_ips:
            imported_floating_ip = self._backend_floating_ip_to_floating_ip(
                backend_ip, tenant
            )
            floating_ip = floating_ips.pop(imported_floating_ip.backend_id, None)
            if floating_ip is None:
                imported_floating_ip.save()
                continue
            if floating_ip.state not in (
                CoreStates.OK,
                CoreStates.ERRED,
            ):
                logger.debug(
                    "Skipping floating IP %s pull because it is not OK or ERRED",
                    imported_floating_ip.backend_id,
                )
                continue

            # Don't update user defined name.
            if floating_ip.address != floating_ip.name:
                imported_floating_ip.name = floating_ip.name
            update_pulled_fields(
                floating_ip,
                imported_floating_ip,
                models.FloatingIP.get_backend_fields(),
            )
            handle_resource_update_success(floating_ip)

    def _backend_floating_ip_to_floating_ip(
        self, backend_floating_ip, tenant: models.Tenant
    ):
        port_id = backend_floating_ip["port_id"]
        if port_id:
            port = models.Port.objects.filter(backend_id=port_id, tenant=tenant).first()
        else:
            port = None
        floating_ip = models.FloatingIP(
            name=backend_floating_ip["floating_ip_address"],
            description=backend_floating_ip.get("description") or "",
            address=backend_floating_ip["floating_ip_address"],
            backend_network_id=backend_floating_ip["floating_network_id"],
            runtime_state=backend_floating_ip["status"],
            backend_id=backend_floating_ip["id"],
            state=CoreStates.OK,
            port=port,
            tenant=tenant,
            service_settings=tenant.service_settings,
            project=tenant.project,
        )

        return floating_ip

    def pull_security_group(self, local_security_group: models.SecurityGroup):
        session = get_tenant_session(local_security_group.tenant)
        neutron = get_neutron_client(session)
        try:
            remote_security_group = neutron.show_security_group(
                local_security_group.backend_id
            )["security_group"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        imported_security_group = self._backend_security_group_to_security_group(
            remote_security_group,
            tenant=local_security_group.tenant,
            service_settings=local_security_group.tenant.service_settings,
            project=local_security_group.tenant.project,
        )

        modified = update_pulled_fields(
            local_security_group,
            imported_security_group,
            models.SecurityGroup.get_backend_fields(),
        )

        if modified:
            self._log_security_group_pulled(local_security_group)

        self._extract_security_group_rules(local_security_group, remote_security_group)
        self._update_remote_security_groups(
            local_security_group.tenant, [remote_security_group]
        )

    def sync_default_security_group(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            backend_security_groups = neutron.list_security_groups(
                tenant_id=tenant.backend_id
            )["security_groups"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        backend_default_group = None
        for backend_group in backend_security_groups:
            if backend_group["name"] == "default":
                backend_default_group = backend_group
        local_default_group: models.SecurityGroup = tenant.security_groups.filter(
            name="default"
        ).first()
        if backend_default_group:
            if local_default_group:
                local_default_group.backend_id = backend_default_group["id"]
                local_default_group.save(update_fields=["backend_id"])
                self.push_security_group_rules(local_default_group)
                local_default_group.set_ok()
                local_default_group.save()
            else:
                self._update_tenant_security_groups(tenant, [backend_default_group])
        else:
            logger.debug(
                "Default security group for tenant %s is not found.", tenant.backend_id
            )

    @log_backend_action("pull security groups for tenant")
    def pull_tenant_security_groups(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            backend_security_groups = neutron.list_security_groups(
                tenant_id=tenant.backend_id
            )["security_groups"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            self._update_tenant_security_groups(tenant, backend_security_groups)
            self._remove_stale_security_groups([tenant], backend_security_groups)

    def _remove_stale_security_groups(self, tenants, backend_security_groups):
        remote_ids = {ip["id"] for ip in backend_security_groups}
        if len(remote_ids) > 0:
            logger.info(f"Remote IDs of detected security groups are: {remote_ids}")

        stale_groups = models.SecurityGroup.objects.filter(
            tenant__in=tenants,
            state__in=[
                CoreStates.OK,
                CoreStates.ERRED,
            ],
        ).exclude(backend_id__in=remote_ids)

        logger.info(f"Removing {stale_groups.count()} sec groups from {tenants}.")
        for security_group in stale_groups:
            event_logger.emit(
                "Security group %s has been cleaned from cache." % security_group.name,
                event_type=EventType.OPENSTACK_SECURITY_GROUP_CLEANED,
                event_context={
                    "security_group": security_group,
                },
                scopes=[security_group, security_group.tenant],
            )
        stale_groups.delete()

    def _update_tenant_security_groups(
        self, tenant: models.Tenant, backend_security_groups
    ):
        for backend_security_group in backend_security_groups:
            imported_security_group = self._backend_security_group_to_security_group(
                backend_security_group,
                tenant=tenant,
                service_settings=tenant.service_settings,
                project=tenant.project,
            )

            try:
                security_group = tenant.security_groups.get(
                    backend_id=imported_security_group.backend_id
                )
            except models.SecurityGroup.DoesNotExist:
                imported_security_group.save()
                security_group = imported_security_group
                self._log_security_group_imported(security_group)
            else:
                if security_group.state not in (
                    CoreStates.OK,
                    CoreStates.ERRED,
                ):
                    logger.info(
                        "Skipping pulling of OpenStack security group because it is "
                        "not in the stable state. Group ID: %s",
                        security_group.id,
                    )
                    continue
                modified = update_pulled_fields(
                    security_group,
                    imported_security_group,
                    models.SecurityGroup.get_backend_fields(),
                )
                handle_resource_update_success(security_group)

                if modified:
                    self._log_security_group_pulled(security_group)

            self._extract_security_group_rules(security_group, backend_security_group)

        self._update_remote_security_groups(tenant, backend_security_groups)

    def _log_security_group_imported(self, security_group: models.SecurityGroup):
        event_logger.emit(
            "Security group %s has been imported to local cache." % security_group.name,
            event_type=EventType.OPENSTACK_SECURITY_GROUP_IMPORTED,
            event_context={"security_group": security_group},
            scopes=[security_group.tenant, security_group],
        )

    def _log_security_group_pulled(self, security_group: models.SecurityGroup):
        event_logger.emit(
            "Security group %s has been pulled from backend." % security_group.name,
            event_type=EventType.OPENSTACK_SECURITY_GROUP_PULLED,
            event_context={"security_group": security_group},
            scopes=[security_group.tenant, security_group],
        )

    def _log_security_group_rule_imported(self, rule: models.SecurityGroupRule):
        # Per-rule events are intentionally not emitted; the aggregate
        # openstack_security_group_rules_changed event covers the whole pull.
        logger.debug("Security group rule %s has been imported from backend.", rule)

    def _log_security_group_rule_pulled(self, rule):
        logger.debug("Security group rule %s has been pulled from backend.", rule)

    def _log_security_group_rule_cleaned(self, rule):
        logger.debug("Security group rule %s has been cleaned from cache.", rule)

    def _update_remote_security_groups(
        self, tenant: models.Tenant, backend_security_groups
    ):
        security_group_map = {
            security_group.backend_id: security_group
            for security_group in models.SecurityGroup.objects.filter(tenant=tenant)
        }
        security_group_rule_map = {
            security_group_rule.backend_id: security_group_rule
            for security_group_rule in models.SecurityGroupRule.objects.filter(
                security_group__tenant=tenant
            )
        }
        for backend_security_group in backend_security_groups:
            for backend_rule in backend_security_group["security_group_rules"]:
                security_group_rule = security_group_rule_map.get(backend_rule["id"])
                remote_group = security_group_map.get(backend_rule["remote_group_id"])
                if not security_group_rule:
                    continue
                if security_group_rule.remote_group != remote_group:
                    security_group_rule.remote_group = remote_group
                    security_group_rule.save(update_fields=["remote_group"])

    def _backend_security_group_to_security_group(
        self, backend_security_group, **kwargs
    ):
        security_group = models.SecurityGroup(
            name=backend_security_group["name"],
            description=backend_security_group["description"],
            backend_id=backend_security_group["id"],
            state=CoreStates.OK,
        )

        for field, value in kwargs.items():
            setattr(security_group, field, value)

        return security_group

    def pull_tenant_routers(self, tenant: models.Tenant, router_backend_id=None):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            if router_backend_id:
                backend_routers = [neutron.show_router(router_backend_id)["router"]]
            else:
                backend_routers = neutron.list_routers(tenant_id=tenant.backend_id)[
                    "routers"
                ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for backend_router in backend_routers:
            backend_id = backend_router["id"]
            try:
                ports = neutron.list_ports(device_id=backend_id)["ports"]
                fixed_ips = []
                for port in ports:
                    for fixed_ip in port["fixed_ips"]:
                        # skip link local addresses
                        if fixed_ip["ip_address"].startswith("169.254") or fixed_ip[
                            "ip_address"
                        ].startswith("fe80::"):
                            continue
                        fixed_ips.append(fixed_ip["ip_address"])
                if backend_router.get("external_gateway_info"):
                    for fixed_ip in backend_router["external_gateway_info"][
                        "external_fixed_ips"
                    ]:
                        fixed_ips.append(fixed_ip["ip_address"])
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

            # Sync external gateway fields
            gateway_info = backend_router.get("external_gateway_info")
            if gateway_info:
                ext_net_id = gateway_info.get("network_id", "")
                ext_net_ref = models.ExternalNetwork.objects.filter(
                    settings=tenant.service_settings, backend_id=ext_net_id
                ).first()
                ext_enable_snat = gateway_info.get("enable_snat")
                ext_fixed_ips = gateway_info.get("external_fixed_ips", [])
            else:
                ext_net_id = ""
                ext_net_ref = None
                ext_enable_snat = None
                ext_fixed_ips = []

            defaults = {
                "name": backend_router["name"],
                "description": backend_router["description"],
                "routes": backend_router["routes"],
                "fixed_ips": fixed_ips,
                "external_network_id": ext_net_id,
                "external_network_ref": ext_net_ref,
                "enable_snat": ext_enable_snat,
                "external_fixed_ips": ext_fixed_ips,
                "service_settings": tenant.service_settings,
                "project": tenant.project,
            }
            # Only assign state when the row is new or recovering from ERRED;
            # never overwrite a transitional state (DELETION_SCHEDULED,
            # DELETING, UPDATE_SCHEDULED, UPDATING, CREATION_SCHEDULED,
            # CREATING) because the worker that issued the transition is
            # racing with this pull and a stale OK write breaks the FSM.
            existing_state = (
                models.Router.objects.filter(tenant=tenant, backend_id=backend_id)
                .values_list("state", flat=True)
                .first()
            )
            if existing_state in (None, CoreStates.ERRED):
                defaults["state"] = CoreStates.OK
            try:
                router_obj, _ = models.Router.objects.update_or_create(
                    tenant=tenant, backend_id=backend_id, defaults=defaults
                )
                # Set the ports relationship
                port_backend_ids = [port["id"] for port in ports]
                port_objs = list(
                    models.Port.objects.filter(
                        tenant=tenant, backend_id__in=port_backend_ids
                    )
                )
                router_obj.ports.set(port_objs)
            except IntegrityError:
                logger.warning(
                    "Could not create router with backend ID %s "
                    "and tenant %s due to concurrent update.",
                    backend_id,
                    tenant,
                )

        if not router_backend_id:
            remote_ids = {ip["id"] for ip in backend_routers}
            stale_routers = models.Router.objects.filter(tenant=tenant).exclude(
                backend_id__in=remote_ids
            )
            stale_routers.delete()

    def _tenant_mappings(self, queryset):
        rows = queryset.exclude(backend_id="").values("id", "backend_id")
        return {row["backend_id"]: row["id"] for row in rows}

    def _port_pull_mappings(self, tenant: models.Tenant):
        return (
            self._tenant_mappings(tenant.available_networks),
            self._tenant_mappings(tenant.available_subnets),
            self._tenant_mappings(models.SecurityGroup.objects.filter(tenant=tenant)),
            self._tenant_mappings(models.Instance.objects.filter(tenant=tenant)),
        )

    def _upsert_port_from_neutron_dict(
        self,
        tenant: models.Tenant,
        backend_port: dict,
        network_mapping: dict,
        subnet_mapping: dict,
        security_group_mapping: dict,
        instance_mapping: dict,
    ):
        """Create or update a Waldur Port from a Neutron port dict (same as pull_tenant_ports)."""
        backend_id = backend_port["id"]

        subnet_id = None
        try:
            subnet_backend_id = backend_port["fixed_ips"][0]["subnet_id"]
            subnet_id = subnet_mapping.get(subnet_backend_id)
        except (AttributeError, KeyError, IndexError):
            pass

        device_id = backend_port.get("device_id")
        instance_id = instance_mapping.get(device_id)

        defaults = {
            "name": backend_port["name"],
            "description": backend_port["description"],
            "service_settings": tenant.service_settings,
            "project": tenant.project,
            "instance_id": instance_id,
            "subnet_id": subnet_id,
            "state": CoreStates.OK,
            "mac_address": backend_port["mac_address"],
            "fixed_ips": backend_port["fixed_ips"],
            "allowed_address_pairs": backend_port.get("allowed_address_pairs", []),
            "network_id": network_mapping.get(backend_port["network_id"]),
            "device_id": device_id,
            "status": backend_port["status"],
            "admin_state_up": backend_port["admin_state_up"],
            "device_owner": backend_port.get("device_owner"),
            "port_security_enabled": backend_port.get("port_security_enabled", True),
        }
        try:
            port, _ = models.Port.objects.update_or_create(
                tenant=tenant, backend_id=backend_id, defaults=defaults
            )
            local_groups = set(
                port.security_groups.values_list("backend_id", flat=True)
            )
            remote_groups = set(backend_port["security_groups"])

            new_groups = remote_groups - local_groups
            for group_id in new_groups:
                local_group_id = security_group_mapping.get(group_id)
                if local_group_id:
                    port.security_groups.add(local_group_id)

            stale_groups = local_groups - remote_groups
            for group in port.security_groups.filter(backend_id__in=stale_groups):
                port.security_groups.remove(group)
            return port
        except IntegrityError:
            logger.warning(
                "Could not create or update port with backend ID %s "
                "and tenant %s due to concurrent update.",
                backend_id,
                tenant,
            )
            return None

    def import_port_from_neutron_by_id(
        self, tenant: models.Tenant, neutron_port_id: str
    ):
        """Fetch one port from Neutron and upsert local Port (same mapping as pull_tenant_ports)."""
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        candidates = [neutron_port_id]
        try:
            normalized = str(uuid.UUID(str(neutron_port_id).strip()))
            if normalized not in candidates:
                candidates.append(normalized)
        except (ValueError, TypeError):
            pass

        backend_port = None
        last_error = None
        for pid in candidates:
            try:
                backend_port = neutron.show_port(pid)["port"]
                break
            except neutron_exceptions.NeutronClientException as e:
                last_error = e
                continue
        if backend_port is None:
            logger.warning(
                "Could not fetch Neutron port %s for tenant %s: %s",
                neutron_port_id,
                tenant,
                last_error,
            )
            return None

        mappings = self._port_pull_mappings(tenant)
        return self._upsert_port_from_neutron_dict(tenant, backend_port, *mappings)

    def update_instance_port_status(self, instance: models.Instance):
        neutron = get_neutron_client(self.admin_session)

        for port in instance.ports.all():
            backend_port = neutron.show_port(port.backend_id)["port"]

            # Update all relevant port status fields
            port.status = backend_port["status"]
            port.device_id = backend_port.get("device_id")
            port.device_owner = backend_port.get("device_owner", "")
            port.admin_state_up = backend_port.get("admin_state_up")
            port.save(
                update_fields=["status", "device_id", "device_owner", "admin_state_up"]
            )

            logger.info(
                "Updated port %s status: %s, device_id: %s, device_owner: %s, admin_state_up: %s",
                port.uuid,
                port.status,
                port.device_id,
                port.device_owner,
                port.admin_state_up,
            )

    def pull_tenant_ports(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            backend_ports = neutron.list_ports(tenant_id=tenant.backend_id)["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        mappings = self._port_pull_mappings(tenant)

        for backend_port in backend_ports:
            self._upsert_port_from_neutron_dict(tenant, backend_port, *mappings)

        remote_ids = {ip["id"] for ip in backend_ports}
        # Only consider ports in a stable state. Ports in CREATION_SCHEDULED /
        # CREATING (saved by OpenStackInstanceSerializer and waiting for
        # create_instance_ports to push them to Neutron) and ports being torn
        # down (DELETION_SCHEDULED / DELETING) must survive this sweep —
        # otherwise the periodic pull races with the executor chain and
        # silently deletes in-flight rows. Same idiom as
        # _remove_stale_floating_ips / _remove_stale_security_groups /
        # stale_networks / stale_subnets / _remove_stale_server_groups.
        stale_ports = (
            models.Port.objects.filter(
                tenant=tenant,
                state__in=[CoreStates.OK, CoreStates.ERRED],
            )
            .exclude(backend_id="")
            .exclude(backend_id__in=remote_ids)
        )
        stale_ports.delete()

    def pull_tenant_networks(self, tenant: models.Tenant):
        """
        Synchronize networks visible to a tenant, handling RBAC shared networks.

        For RBAC environments, this method correctly identifies the true owner
        of each network using the tenant_id from the OpenStack API response,
        preventing incorrect ownership assignment and cyclic deletion issues.

        Args:
            tenant: The tenant to synchronize networks for

        Returns:
            List of networks visible to the tenant
        """
        if not tenant.backend_id:
            return []
        # list_networks for a specific tenant returns all networks *visible* to it,
        # including its own and those shared with it via RBAC.
        backend_networks = self.list_networks(tenant.backend_id)
        visible_networks = []

        with transaction.atomic():
            for backend_network in backend_networks:
                owner_tenant_backend_id = backend_network.get("tenant_id")
                try:
                    # Find the actual owner of the network in Waldur's database.
                    # This is crucial for correct ownership assignment.
                    owner_tenant = models.Tenant.objects.get(
                        service_settings=tenant.service_settings,
                        backend_id=owner_tenant_backend_id,
                    )
                except models.Tenant.DoesNotExist:
                    # The network's owner is not managed by Waldur, or belongs to another provider.
                    # Skip this network to avoid creating orphaned resources.
                    logger.warning(
                        "Skipping network %s sync because its owner tenant %s is not found in Waldur.",
                        backend_network.get("id", "unknown"),
                        owner_tenant_backend_id,
                    )
                    continue

                imported_network = self._backend_network_to_network(
                    backend_network,
                    tenant=owner_tenant,
                    service_settings=owner_tenant.service_settings,
                    project=owner_tenant.project,
                )

                try:
                    # Perform a global lookup to find the network, which is robust against
                    # incorrect tenant associations.
                    network = models.Network.objects.get(
                        service_settings=tenant.service_settings,
                        backend_id=imported_network.backend_id,
                    )
                except models.Network.DoesNotExist:
                    # Network does not exist; create it with the correct owner.
                    imported_network.save()
                    network = imported_network

                    event_logger.emit(
                        "Network %s has been imported to local cache." % network.name,
                        event_type=EventType.OPENSTACK_NETWORK_IMPORTED,
                        event_context={"network": network},
                        scopes=[network, network.tenant],
                    )
                else:
                    # Network exists. Ensure its tenant is correct and update fields.
                    if network.tenant != owner_tenant:
                        network.tenant = owner_tenant
                        network.project = owner_tenant.project
                        # service_settings is already correct due to the lookup key

                    modified = update_pulled_fields(
                        network, imported_network, models.Network.get_backend_fields()
                    )
                    handle_resource_update_success(network)
                    if modified:
                        event_logger.emit(
                            "Network %s has been pulled from backend." % network.name,
                            event_type=EventType.OPENSTACK_NETWORK_PULLED,
                            event_context={"network": network},
                            scopes=[network, network.tenant],
                        )
                visible_networks.append(network)

            # This part correctly cleans up networks *truly owned* by the synced tenant.
            # Shared networks are correctly excluded because their owner is different.
            visible_network_ids = [n.id for n in visible_networks]
            stale_networks = models.Network.objects.filter(
                state__in=[CoreStates.OK, CoreStates.ERRED],
                tenant=tenant,
            ).exclude(id__in=visible_network_ids)

            for network in stale_networks:
                event_logger.emit(
                    "Network %s has been cleaned from cache." % network.name,
                    event_type=EventType.OPENSTACK_NETWORK_CLEANED,
                    event_context={"network": network},
                    scopes=[network, network.tenant],
                )
            stale_networks.delete()

        return visible_networks

    @method_decorator(create_batch_fetcher)
    def list_networks(self, tenant_id: str):
        neutron = get_neutron_client(self.admin_session)
        try:
            return neutron.list_networks(tenant_id=tenant_id)["networks"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    def _backend_network_to_network(self, backend_network, **kwargs):
        network = models.Network(
            name=backend_network["name"],
            description=backend_network["description"],
            is_external=backend_network["router:external"],
            runtime_state=backend_network["status"],
            mtu=backend_network.get("mtu"),
            port_security_enabled=backend_network.get("port_security_enabled", True),
            backend_id=backend_network["id"],
            state=CoreStates.OK,
        )
        if backend_network.get("provider:network_type"):
            network.type = backend_network["provider:network_type"]
        if backend_network.get("provider:segmentation_id"):
            network.segmentation_id = backend_network["provider:segmentation_id"]

        for field, value in kwargs.items():
            setattr(network, field, value)

        return network

    def pull_tenant_subnets(self, tenant: models.Tenant):
        self.pull_subnets(tenant)
        self.pull_shared_subnets()

    def pull_subnets(self, tenant: models.Tenant | None = None, network=None):
        neutron = get_neutron_client(self.admin_session)

        if tenant:
            networks = tenant.networks.all()
        elif network:
            networks = [network]
        else:
            networks = models.Network.objects.filter(
                state=CoreStates.OK,
                service_settings=self.settings,
            )
        network_mappings = {network.backend_id: network for network in networks}
        if not network_mappings:
            return

        try:
            if tenant:
                backend_subnets = neutron.list_subnets(tenant_id=tenant.backend_id)[
                    "subnets"
                ]
            elif network:
                backend_subnets = neutron.list_subnets(network_id=network.backend_id)[
                    "subnets"
                ]
            else:
                # We can't filter subnets by network IDs because it exceeds maximum request length
                backend_subnets = neutron.list_subnets()["subnets"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        subnet_uuids = []
        with transaction.atomic():
            for backend_subnet in backend_subnets:
                network = network_mappings.get(backend_subnet["network_id"])

                if not network:
                    logger.debug(
                        "OpenStack network is not imported yet. Network ID: %s",
                        backend_subnet["network_id"],
                    )
                    continue

                imported_subnet = self._backend_subnet_to_subnet(
                    backend_subnet,
                    network=network,
                    service_settings=network.service_settings,
                    project=network.project,
                    tenant=network.tenant,
                )

                try:
                    subnet = models.SubNet.objects.get(
                        network=network, backend_id=imported_subnet.backend_id
                    )
                except models.SubNet.DoesNotExist:
                    imported_subnet.save()
                    subnet = imported_subnet

                    event_logger.emit(
                        "SubNet %s has been imported to local cache." % subnet.name,
                        event_type=EventType.OPENSTACK_SUBNET_IMPORTED,
                        event_context={
                            "subnet": subnet,
                        },
                        scopes=[subnet, subnet.network],
                    )

                else:
                    modified = update_pulled_fields(
                        subnet, imported_subnet, models.SubNet.get_backend_fields()
                    )
                    handle_resource_update_success(subnet)
                    if modified:
                        event_logger.emit(
                            "SubNet %s has been pulled from backend." % subnet.name,
                            event_type=EventType.OPENSTACK_SUBNET_PULLED,
                            event_context={
                                "subnet": subnet,
                            },
                            scopes=[subnet, subnet.network],
                        )

                subnet_uuids.append(subnet.uuid)

            stale_subnets = models.SubNet.objects.filter(
                state__in=[CoreStates.OK, CoreStates.ERRED],
                network__in=networks,
            ).exclude(uuid__in=subnet_uuids)
            for subnet in stale_subnets:
                event_logger.emit(
                    "SubNet %s has been cleaned." % subnet.name,
                    event_type=EventType.OPENSTACK_SUBNET_CLEANED,
                    event_context={
                        "subnet": subnet,
                    },
                    scopes=[subnet, subnet.network],
                )
            stale_subnets.delete()

    def pull_shared_subnets(self):
        """Synchronize external/shared subnets"""
        neutron = get_neutron_client(self.admin_session)

        try:
            external_networks = neutron.list_networks(
                **{"router:external": True, "shared": True}
            )["networks"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for ext_network in external_networks:
            try:
                network = models.Network.objects.get(backend_id=ext_network["id"])
            except models.Network.DoesNotExist:
                continue

            # Now sync subnets for this network
            self.pull_subnets(network=network)

    def _backend_subnet_to_subnet(self, backend_subnet, **kwargs):
        subnet = models.SubNet(
            name=backend_subnet["name"],
            description=backend_subnet["description"],
            allocation_pools=backend_subnet.get("allocation_pools"),
            cidr=backend_subnet["cidr"],
            ip_version=backend_subnet["ip_version"],
            enable_dhcp=backend_subnet["enable_dhcp"],
            gateway_ip=backend_subnet.get("gateway_ip"),
            dns_nameservers=backend_subnet["dns_nameservers"],
            host_routes=sorted(
                backend_subnet.get("host_routes", []), key=lambda x: tuple(x.values())
            ),
            backend_id=backend_subnet["id"],
            state=CoreStates.OK,
        )

        for field, value in kwargs.items():
            setattr(subnet, field, value)

        return subnet

    @log_backend_action()
    def create_tenant(self, tenant: models.Tenant):
        session = get_keystone_session(tenant.service_settings)
        keystone = get_keystone_client(session)
        try:
            backend_tenant = keystone.projects.create(
                name=tenant.name,
                description=tenant.description,
                domain=self._get_domain(),
            )
            tenant.backend_id = backend_tenant.id
            tenant.save(update_fields=["backend_id"])
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_tenant_safe(self, tenant: models.Tenant):
        """
        Check available tenant name before creating tenant.
        It allows to avoid failure when name is already taken.
        """
        new_name = self.get_available_tenant_name(tenant.name)
        if new_name != tenant.name:
            tenant.name = new_name
            tenant.save(update_fields=["name"])
        self.create_tenant(tenant)

    def get_available_tenant_name(self, name, max_length=64):
        """
        Returns a tenant name that's free on the target deployment.
        """
        keystone = get_keystone_client(self.admin_session)
        try:
            tenants = keystone.projects.list(domain=self._get_domain())
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        names = [tenant.name for tenant in tenants]
        new_name = name

        # If the tenant name already exists, add an underscore and a random 3
        # character alphanumeric string to the tenant name until the generated name doesn't exist.
        # Truncate original name if required, so the new name does not exceed the max_length.
        while new_name in names:
            new_name = f"{name}_{get_random_string(3)}"
            truncation = len(new_name) - max_length
            if truncation > 0:
                new_name = f"{name[:-truncation]}_{get_random_string(3)}"
        return new_name

    def _import_tenant(
        self, tenant_backend_id, service_settings=None, project=None, save=True
    ):
        keystone = get_keystone_client(self.admin_session)
        try:
            backend_tenant = keystone.projects.get(tenant_backend_id)
        except NotFound as e:
            raise OpenStackTenantNotFound(e)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        tenant = models.Tenant()
        tenant.name = backend_tenant.name
        tenant.description = backend_tenant.description
        tenant.backend_id = tenant_backend_id

        if save and service_settings:
            tenant.service_settings = service_settings
            tenant.project = project
            tenant.state = CoreStates.OK
            tenant.save()
        return tenant

    def import_tenant(self, backend_id, project):
        tenant = self._import_tenant(backend_id, self.settings, project)
        tenant.user_username = models.Tenant.generate_username(tenant.name)
        tenant.user_password = pwgen()
        tenant.save()
        return tenant

    def get_importable_tenants(self):
        keystone = get_keystone_client(self.admin_session)
        try:
            tenants = [
                {
                    "type": get_resource_type(models.Tenant),
                    "name": tenant.name,
                    "description": tenant.description,
                    "backend_id": tenant.id,
                }
                for tenant in keystone.projects.list(domain=self._get_domain())
            ]
            return self.get_importable_resources(models.Tenant, tenants)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def pull_tenant(self, tenant: models.Tenant):
        import_time = timezone.now()
        imported_tenant = self._import_tenant(tenant.backend_id, save=False)

        tenant.refresh_from_db()
        # if tenant was not modified in Waldur database after import.
        if tenant.modified < import_time:
            update_pulled_fields(tenant, imported_tenant, ("name", "description"))

    @log_backend_action()
    def does_tenant_exist_in_backend(self, tenant: models.Tenant):
        try:
            self._import_tenant(tenant.backend_id, save=False)
        except OpenStackTenantNotFound:
            return False
        except Exception as e:
            logger.error(
                "Checking for tenant %s availability caused an error %s.",
                tenant,
                e,
            )
            return None
        return True

    @log_backend_action()
    def add_admin_user_to_tenant(self, tenant: models.Tenant):
        """Add user from openstack settings to new tenant"""
        session = get_keystone_session(tenant.service_settings)
        keystone = get_keystone_client(session)

        try:
            admin_user = keystone.users.find(name=self.settings.username)
            admin_role = keystone.roles.find(name="admin")
            try:
                keystone.roles.grant(
                    user=admin_user.id, role=admin_role.id, project=tenant.backend_id
                )
            except keystone_exceptions.Conflict:
                pass
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action("add user to tenant")
    def create_tenant_user(self, tenant: models.Tenant):
        keystone = get_keystone_client(self.admin_session)

        try:
            user = keystone.users.create(
                name=tenant.user_username,
                password=tenant.user_password,
                domain=self._get_domain(),
            )
            try:
                role = keystone.roles.find(name="Member")
            except keystone_exceptions.NotFound:
                role = keystone.roles.find(name="_member_")
            keystone.roles.grant(
                user=user.id,
                role=role.id,
                project=tenant.backend_id,
            )
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def create_or_update_tenant_user(self, tenant: models.Tenant):
        keystone = get_keystone_client(self.admin_session)

        try:
            keystone_user = keystone.users.find(name=tenant.user_username)
        except keystone_exceptions.NotFound:
            self.create_tenant_user(tenant)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            self.change_tenant_user_password(tenant, keystone_user)

    @log_backend_action("change password for tenant user")
    def change_tenant_user_password(self, tenant: models.Tenant, keystone_user=None):
        keystone = get_keystone_client(self.admin_session)

        try:
            if not keystone_user:
                keystone_user = keystone.users.find(name=tenant.user_username)
            keystone.users.update(user=keystone_user, password=tenant.user_password)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_floating_ips(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            floatingips = neutron.list_floatingips(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for floating_ip in floatingips.get("floatingips", []):
            self._delete_backend_floating_ip(
                tenant, floating_ip["id"], tenant.backend_id
            )

    @log_backend_action()
    def delete_tenant_ports(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            ports = neutron.list_ports(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for port in ports.get("ports", []):
            if (
                "device_id" in port
                and port["device_owner"] in VALID_ROUTER_INTERFACE_OWNERS
            ):
                logger.info(
                    "Deleting port %s interface_router from tenant %s",
                    port["id"],
                    tenant.backend_id,
                )
                try:
                    neutron.remove_interface_router(
                        port["device_id"], {"port_id": port["id"]}
                    )
                except neutron_exceptions.NotFound:
                    logger.debug(
                        "Port %s interface_router is already gone from tenant %s",
                        port["id"],
                        tenant.backend_id,
                    )
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)

            logger.info(
                "Deleting port %s from tenant %s", port["id"], tenant.backend_id
            )
            try:
                neutron.delete_port(port["id"])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Port %s is already gone from tenant %s",
                    port["id"],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_routes(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for router in routers.get("routers", []):
            if not router["routes"]:
                continue
            logger.info(
                "Deleting routes for router %s from tenant %s",
                router["id"],
                tenant.backend_id,
            )
            try:
                neutron.update_router(router["id"], {"router": {"routes": []}})
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_routers(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for router in routers.get("routers", []):
            logger.info(
                "Deleting router %s from tenant %s", router["id"], tenant.backend_id
            )
            try:
                neutron.delete_router(router["id"])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Router %s is already gone from tenant %s",
                    router["id"],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_networks(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            networks = neutron.list_networks(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for network in networks.get("networks", []):
            if network["router:external"]:
                continue
            for subnet in network["subnets"]:
                logger.info(
                    "Deleting subnetwork %s from tenant %s", subnet, tenant.backend_id
                )
                try:
                    neutron.delete_subnet(subnet)
                except neutron_exceptions.NotFound:
                    logger.info(
                        "Subnetwork %s is already gone from tenant %s",
                        subnet,
                        tenant.backend_id,
                    )
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)

            logger.info(
                "Deleting network %s from tenant %s", network["id"], tenant.backend_id
            )
            try:
                neutron.delete_network(network["id"])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Network %s is already gone from tenant %s",
                    network["id"],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

        tenant.set_quota_usage("network_count", 0)
        tenant.set_quota_usage("subnet_count", 0)

    @log_backend_action()
    def delete_tenant_security_groups(self, tenant: models.Tenant):
        neutron = get_neutron_client(self.admin_session)

        try:
            sgroups = neutron.list_security_groups(tenant_id=tenant.backend_id)[
                "security_groups"
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for sgroup in sgroups:
            logger.info(
                "Deleting security group %s from tenant %s",
                sgroup["id"],
                tenant.backend_id,
            )
            try:
                neutron.delete_security_group(sgroup["id"])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Security group %s is already gone from tenant %s",
                    sgroup["id"],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_instances(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)

        try:
            servers = nova.servers.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        for server in servers:
            logger.info(
                "Deleting instance %s from tenant %s", server.id, tenant.backend_id
            )
            try:
                server.delete()
            except nova_exceptions.NotFound:
                logger.debug(
                    "Instance %s is already gone from tenant %s",
                    server.id,
                    tenant.backend_id,
                )
            except nova_exceptions.ClientException as e:
                raise OpenStackBackendError(e)

    def are_all_tenant_instances_deleted(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)

        try:
            servers = nova.servers.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return not servers

    @log_backend_action()
    def delete_tenant_snapshots(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)

        try:
            snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        for snapshot in snapshots:
            logger.info(
                "Deleting snapshot %s from tenant %s", snapshot.id, tenant.backend_id
            )
            try:
                snapshot.delete()
            except cinder_exceptions.NotFound:
                logger.debug(
                    "Snapshot %s is already gone from tenant %s",
                    snapshot.id,
                    tenant.backend_id,
                )
            except cinder_exceptions.ClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def are_all_tenant_snapshots_deleted(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)

        try:
            snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return not snapshots

    @log_backend_action()
    def delete_tenant_volumes(self, tenant: models.Tenant):
        cinder = get_cinder_client(self.admin_session)

        try:
            volumes = cinder.volumes.list(
                search_opts={"project_id": tenant.backend_id, "all_tenants": 1}
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        for volume in volumes:
            logger.info(
                "Deleting volume %s from tenant %s", volume.id, tenant.backend_id
            )
            try:
                volume.force_delete()
            except cinder_exceptions.NotFound:
                logger.debug(
                    "Volume %s is already gone from tenant %s",
                    volume.id,
                    tenant.backend_id,
                )
            except cinder_exceptions.ClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def are_all_tenant_volumes_deleted(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)

        try:
            volumes = cinder.volumes.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return not volumes

    @log_backend_action()
    def delete_tenant_user(self, tenant: models.Tenant):
        keystone = get_keystone_client(self.admin_session)
        try:
            user = keystone.users.find(name=tenant.user_username)
            logger.info(
                "Deleting user %s that was connected to tenant %s",
                user.name,
                tenant.backend_id,
            )
            user.delete()
        except keystone_exceptions.NotFound:
            logger.debug(
                "User %s is already gone from tenant %s",
                tenant.user_username,
                tenant.backend_id,
            )
        except keystone_exceptions.ClientException as e:
            logger.error(
                "Cannot delete user %s from tenant %s. Error: %s",
                tenant.user_username,
                tenant.backend_id,
                e,
            )

    @log_backend_action()
    def delete_tenant(self, tenant: models.Tenant):
        if not tenant.backend_id:
            raise OpenStackBackendError(
                "This method should not be called if tenant has no backend_id"
            )

        session = get_keystone_session(tenant.service_settings)
        keystone = get_keystone_client(session)

        logger.info("Deleting tenant %s", tenant.backend_id)
        try:
            keystone.projects.delete(tenant.backend_id)
        except keystone_exceptions.NotFound:
            logger.debug("Tenant %s is already gone", tenant.backend_id)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_server_groups(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)

        try:
            server_groups = nova.server_groups.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        for server_group in server_groups:
            logger.info(
                "Deleting server group %s from tenant %s",
                server_group.id,
                tenant.backend_id,
            )
            try:
                server_group.delete()
            except nova_exceptions.NotFound:
                logger.debug(
                    "Server group %s is already gone from tenant %s",
                    server_group.id,
                    tenant.backend_id,
                )
            except nova_exceptions.ClientException as e:
                raise OpenStackBackendError(e)

    def _normalize_security_group_rule(self, rule):
        if rule["protocol"] is None:
            rule["protocol"] = ""

        if rule["port_range_min"] is None:
            rule["port_range_min"] = -1

        if rule["port_range_max"] is None:
            rule["port_range_max"] = -1

        return rule

    def _extract_security_group_rules(self, security_group, backend_security_group):
        backend_rules = backend_security_group["security_group_rules"]
        # Snapshot local state before pull, for the aggregate diff event.
        old_snapshot = audit.snapshot_security_group_rules(security_group)

        cur_rules = {rule.backend_id: rule for rule in security_group.rules.all()}
        for backend_rule in backend_rules:
            cur_rules.pop(backend_rule["id"], None)
            backend_rule = self._normalize_security_group_rule(backend_rule)
            rule, created = security_group.rules.update_or_create(
                backend_id=backend_rule["id"],
                defaults=self._import_security_group_rule(backend_rule),
            )
            if created:
                self._log_security_group_rule_imported(rule)
            else:
                self._log_security_group_rule_pulled(rule)
        stale_rules = security_group.rules.filter(backend_id__in=cur_rules.keys())
        for rule in stale_rules:
            self._log_security_group_rule_cleaned(rule)
        stale_rules.delete()

        # Emit one aggregate event covering the entire pull reconciliation.
        new_snapshot = audit.snapshot_security_group_rules(security_group)
        diff = compute_collection_diff(
            old_snapshot,
            new_snapshot,
            identity_key=lambda r: r["_pk"],
            compare_fields=audit.SECURITY_GROUP_RULE_COMPARE_FIELDS,
            serialize=lambda r: {k: v for k, v in r.items() if k != "_pk"},
        )
        audit.emit_security_group_rules_changed(
            security_group, diff, trigger="backend_sync"
        )

    def _import_security_group_rule(self, backend_rule):
        return {
            "ethertype": backend_rule["ethertype"],
            "direction": backend_rule["direction"],
            "from_port": backend_rule["port_range_min"],
            "to_port": backend_rule["port_range_max"],
            "protocol": backend_rule["protocol"],
            "cidr": backend_rule["remote_ip_prefix"],
            "description": backend_rule["description"] or "",
        }

    @log_backend_action()
    def push_security_group_rules(self, security_group: models.SecurityGroup):
        session = get_tenant_session(security_group.tenant)
        neutron = get_neutron_client(session)

        try:
            backend_security_group = neutron.show_security_group(
                security_group.backend_id
            )["security_group"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        backend_rules = {
            rule["id"]: self._normalize_security_group_rule(rule)
            for rule in backend_security_group["security_group_rules"]
        }

        # list of waldur rules, that do not exist in openstack
        nonexistent_rules = []
        # list of waldur rules, that have wrong parameters in openstack
        unsynchronized_rules = []
        # list of os rule ids, that exist in openstack and do not exist in waldur
        extra_rule_ids = list(backend_rules.keys())

        for local_rule in security_group.rules.all():
            if local_rule.backend_id not in backend_rules:
                nonexistent_rules.append(local_rule)
            else:
                backend_rule = backend_rules[local_rule.backend_id]
                if not self._are_rules_equal(backend_rule, local_rule):
                    unsynchronized_rules.append(local_rule)
                extra_rule_ids.remove(local_rule.backend_id)

        # deleting extra rules
        for backend_rule_id in extra_rule_ids:
            logger.debug(
                "About to delete security group rule with id %s in backend",
                backend_rule_id,
            )
            try:
                neutron.delete_security_group_rule(backend_rule_id)
            except neutron_exceptions.NeutronClientException:
                logger.exception(
                    "Failed to remove rule with id %s from security group %s in backend",
                    backend_rule_id,
                    security_group,
                )
            else:
                logger.info(
                    "Security group rule with id %s successfully deleted in backend",
                    backend_rule_id,
                )
                # Per-rule events intentionally omitted — the aggregate
                # openstack_security_group_rules_changed event (emitted at
                # the API or pull layer) covers the change.

        # deleting unsynchronized rules
        for local_rule in unsynchronized_rules:
            logger.debug(
                "About to delete security group rule with id %s", local_rule.backend_id
            )
            try:
                neutron.delete_security_group_rule(local_rule.backend_id)
            except neutron_exceptions.NeutronClientException:
                logger.exception(
                    "Failed to remove rule with id %s from security group %s in backend",
                    local_rule.backend_id,
                    security_group,
                )
            else:
                logger.info(
                    "Security group rule with id %s successfully deleted in backend",
                    local_rule.backend_id,
                )
                # Per-rule events intentionally omitted (see above).

        # creating nonexistent and unsynchronized rules
        for local_rule in unsynchronized_rules + nonexistent_rules:
            logger.debug(
                "About to create security group rule with id %s in backend",
                local_rule.id,
            )
            try:
                # The database has empty strings instead of nulls
                if local_rule.protocol == "":
                    local_rule_protocol = None
                else:
                    local_rule_protocol = local_rule.protocol

                sec_group_rule = neutron.create_security_group_rule(
                    {
                        "security_group_rule": {
                            "security_group_id": security_group.backend_id,
                            "ethertype": local_rule.ethertype,
                            "direction": local_rule.direction,
                            "protocol": local_rule_protocol,
                            "port_range_min": local_rule.from_port
                            if local_rule.from_port != -1
                            else None,
                            "port_range_max": local_rule.to_port
                            if local_rule.to_port != -1
                            else None,
                            "remote_ip_prefix": local_rule.cidr,
                            "remote_group_id": local_rule.remote_group.backend_id
                            if local_rule.remote_group
                            else None,
                            "description": local_rule.description,
                        }
                    }
                )

                new_backend_id = sec_group_rule["security_group_rule"]["id"]
                if new_backend_id != local_rule.backend_id:
                    local_rule.backend_id = new_backend_id
                    local_rule.save(update_fields=["backend_id"])
            except neutron_exceptions.NeutronClientException as e:
                logger.exception(
                    "Failed to create rule %s for security group %s in backend",
                    local_rule,
                    security_group,
                )
                raise OpenStackBackendError(e)
            else:
                logger.info(
                    "Security group rule with id %s successfully created in backend",
                    local_rule.id,
                )
                # Per-rule events intentionally omitted (see above).

    @log_backend_action()
    def create_security_group(self, security_group: models.SecurityGroup):
        session = get_tenant_session(security_group.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_security_group = neutron.create_security_group(
                {
                    "security_group": {
                        "name": security_group.name,
                        "description": security_group.description,
                    }
                }
            )["security_group"]
            security_group.backend_id = backend_security_group["id"]
            security_group.save(update_fields=["backend_id"])
            self.push_security_group_rules(security_group)

            event_logger.emit(
                'Security group "%s" has been created in the backend.'
                % security_group.name,
                event_type=EventType.OPENSTACK_SECURITY_GROUP_CREATED,
                event_context={
                    "security_group": security_group,
                },
                scopes=[security_group, security_group.tenant],
            )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_security_group(self, security_group: models.SecurityGroup):
        session = get_tenant_session(security_group.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.delete_security_group(security_group.backend_id)

            event_logger.emit(
                'Security group "%s" has been deleted' % security_group.name,
                event_type=EventType.OPENSTACK_SECURITY_GROUP_DELETED,
                event_context={
                    "security_group": security_group,
                },
                scopes=[security_group, security_group.tenant],
            )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        security_group.decrease_backend_quotas_usage()
        security_group.delete()

    def detach_security_group_from_all_instances(
        self, security_group: models.SecurityGroup
    ):
        connected_instances = self.get_instances_connected_to_security_groups(
            security_group
        )
        for instance_id in connected_instances:
            self.detach_security_group_from_instance(security_group, instance_id)

    def get_instances_connected_to_security_groups(
        self, security_group: models.SecurityGroup
    ):
        session = get_tenant_session(security_group.tenant)
        nova = get_nova_client(session)
        try:
            instances = nova.servers.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        connected_instances = set()
        for instance in instances:
            if hasattr(
                instance, "security_groups"
            ):  # can be missing if instance is being deleted
                for group in instance.security_groups:
                    if security_group.name == group["name"]:
                        connected_instances.add(instance.id)
        return connected_instances

    def detach_security_group_from_instance(
        self, security_group: models.SecurityGroup, server_id: str
    ):
        session = get_tenant_session(security_group.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.remove_security_group(server_id, security_group.backend_id)
        except nova_exceptions.ClientException:
            logger.exception(
                "Failed to remove security group %s from instance %s",
                security_group.backend_id,
                server_id,
            )
        else:
            logger.info(
                "Removed security group %s from instance %s",
                security_group.backend_id,
                server_id,
            )

    def detach_security_group_from_all_ports(
        self, security_group: models.SecurityGroup
    ):
        session = get_tenant_session(security_group.tenant)
        neutron = get_neutron_client(session)
        try:
            remote_ports = neutron.list_ports(
                tenant_id=security_group.tenant.backend_id
            )["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for remote_port in remote_ports:
            # Neutron REST API doesn't allow to filter ports by security groups
            if security_group.backend_id not in remote_port["security_groups"]:
                continue
            security_groups = remote_port["security_groups"]
            security_groups.remove(security_group.backend_id)
            try:
                neutron.update_port(
                    remote_port["id"],
                    {"port": {"security_groups": security_groups}},
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def update_security_group(self, security_group: models.SecurityGroup):
        session = get_tenant_session(security_group.tenant)
        neutron = get_neutron_client(session)
        data = {"name": security_group.name, "description": security_group.description}
        try:
            neutron.update_security_group(
                security_group.backend_id, {"security_group": data}
            )
            self.push_security_group_rules(security_group)

            event_logger.emit(
                'Security group "%s" has been updated' % security_group.name,
                event_type=EventType.OPENSTACK_SECURITY_GROUP_UPDATED,
                event_context={
                    "security_group": security_group,
                },
                scopes=[security_group, security_group.tenant],
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def push_tenant_security_groups(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            backend_groups_list = neutron.list_security_groups(
                tenant_id=tenant.backend_id
            )["security_groups"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        backend_groups = {g["name"]: g for g in backend_groups_list}
        local_groups = {sg.name: sg for sg in tenant.security_groups.all()}

        # 1. Delete groups from backend that are no longer in local DB
        for name, backend_group in backend_groups.items():
            if name not in local_groups and name != "default":
                try:
                    neutron.delete_security_group(backend_group["id"])
                    logger.info(
                        f"Deleted stale security group {name} from backend for tenant {tenant.name}"
                    )
                except neutron_exceptions.NeutronClientException as e:
                    logger.warning(
                        f"Could not delete stale security group {name} from backend: {e}"
                    )

        # 2. Create/update groups and their rules
        for name, local_group in local_groups.items():
            backend_group = backend_groups.get(name)

            if not backend_group:
                # Create group and then its rules
                try:
                    new_backend_group = neutron.create_security_group(
                        {
                            "security_group": {
                                "name": local_group.name,
                                "description": local_group.description,
                            }
                        }
                    )["security_group"]
                    local_group.backend_id = new_backend_group["id"]
                    local_group.save(update_fields=["backend_id"])
                    self.push_security_group_rules(local_group)
                    logger.info(
                        f"Created security group {name} in backend for tenant {tenant.name}"
                    )
                except OpenStackBackendError as e:
                    logger.error(
                        f"Could not create security group {name} in backend: {e}"
                    )
            else:
                # Update group
                if not local_group.backend_id:
                    local_group.backend_id = backend_group["id"]
                    local_group.save(update_fields=["backend_id"])
                try:
                    # update_security_group also calls push_security_group_rules
                    self.update_security_group(local_group)
                except OpenStackBackendError as e:
                    logger.error(
                        f"Could not update security group {name} in backend: {e}"
                    )

    @log_backend_action()
    def create_server_group(self, server_group: models.ServerGroup):
        session = get_tenant_session(server_group.tenant)
        nova = get_nova_client(session)
        try:
            backend_server_group = nova.server_groups.create(
                name=server_group.name, policy=server_group.policy
            )
            server_group.backend_id = backend_server_group.id
            server_group.save(update_fields=["backend_id"])
            event_logger.emit(
                'Server group "%s" has been created in the backend.'
                % server_group.name,
                event_type=EventType.OPENSTACK_SERVER_GROUP_CREATED,
                event_context={
                    "server_group": server_group,
                },
                scopes=[server_group, server_group.tenant],
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_server_group(self, server_group: models.ServerGroup):
        session = get_tenant_session(server_group.tenant)
        nova = get_nova_client(session)
        try:
            nova.server_groups.delete(server_group.backend_id)
            event_logger.emit(
                'Server group "%s" has been deleted' % server_group.name,
                event_type=EventType.OPENSTACK_SERVER_GROUP_DELETED,
                event_context={
                    "server_group": server_group,
                },
                scopes=[server_group, server_group.tenant],
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def set_static_routes(self, router: models.Router):
        session = get_tenant_session(router.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.update_router(
                router.backend_id, {"router": {"routes": router.routes}}
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e.message)

    @log_backend_action()
    def set_external_gateway(self, router: models.Router):
        neutron = get_neutron_client(self.admin_session)
        body = {"network_id": router.external_network_id}
        if router.enable_snat is not None:
            body["enable_snat"] = router.enable_snat
        if router.external_fixed_ips:
            body["external_fixed_ips"] = router.external_fixed_ips
        try:
            neutron.update_router(
                router.backend_id,
                {"router": {"external_gateway_info": body}},
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        self.pull_tenant_routers(router.tenant, router.backend_id)

    @log_backend_action()
    def remove_external_gateway(self, router: models.Router):
        neutron = get_neutron_client(self.admin_session)
        try:
            neutron.update_router(
                router.backend_id,
                {"router": {"external_gateway_info": None}},
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        router.external_network_id = ""
        router.external_network_ref = None
        router.enable_snat = None
        router.external_fixed_ips = []
        router.save(
            update_fields=[
                "external_network_id",
                "external_network_ref",
                "enable_snat",
                "external_fixed_ips",
            ]
        )

    @log_backend_action()
    def detect_external_network(
        self, tenant: models.Tenant, router: models.Router | None = None
    ):
        """
        Detect and recover external network configuration for tenant.
        If no external network is found but one is configured in settings, attempt auto-recovery.
        """
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)["routers"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        # Check if router exists with external gateway
        if bool(routers):
            if router:
                router_backend_id = router.backend_id
                selected_router = None
                for r in routers:
                    if r["id"] == router_backend_id:
                        selected_router = r
                        break

                if not selected_router:
                    logger.warning(
                        "Router %s (backend_id: %s) not found in tenant %s routers. "
                        "Falling back to first router.",
                        router,
                        router_backend_id,
                        tenant,
                    )
                    selected_router = routers[0]
            else:
                selected_router = routers[0]

            ext_gw = selected_router.get("external_gateway_info", {})
            if ext_gw and "network_id" in ext_gw:
                tenant.external_network_id = ext_gw["network_id"]
                # Also set the FK if an ExternalNetwork record exists
                ext_net = models.ExternalNetwork.objects.filter(
                    settings=tenant.service_settings,
                    backend_id=ext_gw["network_id"],
                ).first()
                if ext_net:
                    tenant.external_network_ref = ext_net
                tenant.save()
                logger.info(
                    "Found and set external network with id %s for tenant %s (PK: %s) "
                    "using router %s",
                    ext_gw["network_id"],
                    tenant,
                    tenant.pk,
                    selected_router.get("id", "unknown"),
                )
                return

        # Auto-recovery: Check if external network is configured but not connected
        expected_external_network_id = get_external_network_id(tenant)
        if expected_external_network_id and not tenant.external_network_id:
            logger.info(
                "Attempting auto-recovery: connecting tenant %s (PK: %s) to external network %s",
                tenant,
                tenant.pk,
                expected_external_network_id,
            )
            try:
                # Try to connect to external network
                self.connect_tenant_to_external_network(
                    tenant, expected_external_network_id
                )
                logger.info(
                    "Auto-recovery successful: connected tenant %s (PK: %s) to external network %s",
                    tenant,
                    tenant.pk,
                    expected_external_network_id,
                )
            except Exception as e:
                logger.warning(
                    "Auto-recovery failed for tenant %s (PK: %s): %s. "
                    "Manual intervention may be required.",
                    tenant,
                    tenant.pk,
                    e,
                )
        elif not routers:
            logger.warning(
                "Tenant %s (PK: %s) does not have connected routers and no external network configured.",
                tenant,
                tenant.pk,
            )

    @log_backend_action()
    def create_network(self, network: models.Network):
        session = get_tenant_session(network.tenant)
        neutron = get_neutron_client(session)

        data = {"name": network.name, "tenant_id": network.tenant.backend_id}

        if network.mtu:
            data["mtu"] = network.mtu

        try:
            response = neutron.create_network({"network": data})
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            backend_network = response["network"]
            network.backend_id = backend_network["id"]
            network.runtime_state = backend_network["status"]
            if backend_network.get("provider:network_type"):
                network.type = backend_network["provider:network_type"]
            if backend_network.get("provider:segmentation_id"):
                network.segmentation_id = backend_network["provider:segmentation_id"]
            network.save()
            # XXX: temporary fix - right now backend logic is based on statement "one tenant has one network"
            # We need to fix this in the future.
            network.tenant.internal_network_id = network.backend_id
            network.tenant.save()

            event_logger.emit(
                "Network %s has been created in the backend." % network.name,
                event_type=EventType.OPENSTACK_NETWORK_CREATED,
                event_context={
                    "network": network,
                },
                scopes=[network, network.tenant],
            )

    def _update_network(self, network: models.Network, data):
        session = get_tenant_session(network.tenant)
        neutron = get_neutron_client(session)

        try:
            neutron.update_network(network.backend_id, {"network": data})
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def update_network(self, network: models.Network):
        self._update_network(
            network, {"name": network.name, "description": network.description}
        )
        event_logger.emit(
            f"Network name {network.name} and description {network.description} have been updated.",
            event_type=EventType.OPENSTACK_NETWORK_UPDATED,
            event_context={"network": network},
            scopes=[network, network.tenant],
        )

    @log_backend_action()
    def set_network_mtu(self, network: models.Network):
        self._update_network(network, {"mtu": network.mtu})
        event_logger.emit(
            "Network MTU %s has been updated." % network.name,
            event_type=EventType.OPENSTACK_NETWORK_UPDATED,
            event_context={"network": network},
            scopes=[network, network.tenant],
        )

    @log_backend_action()
    def delete_network(self, network: models.Network):
        for subnet in network.subnets.all():
            self.delete_subnet(subnet)

        session = get_tenant_session(network.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.delete_network(network.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            network.decrease_backend_quotas_usage()
            event_logger.emit(
                "Network %s has been deleted" % network.name,
                event_type=EventType.OPENSTACK_NETWORK_DELETED,
                event_context={
                    "network": network,
                },
                scopes=[network, network.tenant],
            )

    @log_backend_action()
    def import_tenant_networks(self, tenant: models.Tenant):
        networks = self.pull_tenant_networks(tenant)
        if networks:
            # XXX: temporary fix - right now backend logic is based on statement "one tenant has one network"
            # We need to fix this in the future.
            tenant.internal_network_id = networks[0].backend_id
            tenant.save()

    def import_network(self, network: models.Network):
        session = get_tenant_session(network.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_network = neutron.show_network(network.backend_id)["network"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        return self._backend_network_to_network(backend_network)

    @log_backend_action()
    def pull_network(self, network: models.Network):
        import_time = timezone.now()
        imported_network = self.import_network(network)

        network.refresh_from_db()
        if network.modified < import_time:
            modified = update_pulled_fields(
                network, imported_network, models.Network.get_backend_fields()
            )
            if modified:
                event_logger.emit(
                    "Network %s has been pulled from backend." % network.name,
                    event_type=EventType.OPENSTACK_NETWORK_PULLED,
                    event_context={"network": network},
                    scopes=[network, network.tenant],
                )

        self.pull_subnets(network=network)

    @log_backend_action()
    def create_subnet(self, subnet: models.SubNet):
        session = get_tenant_session(subnet.tenant)
        neutron = get_neutron_client(session)

        data = {
            "name": subnet.name,
            "network_id": subnet.network.backend_id,
            "tenant_id": subnet.network.tenant.backend_id,
            "cidr": subnet.cidr,
            "ip_version": subnet.ip_version,
            "enable_dhcp": subnet.enable_dhcp,
        }
        if subnet.allocation_pools:
            data["allocation_pools"] = subnet.allocation_pools
        if subnet.dns_nameservers:
            data["dns_nameservers"] = subnet.dns_nameservers
        if subnet.host_routes:
            data["host_routes"] = subnet.host_routes
        if subnet.disable_gateway:
            data["gateway_ip"] = None
        elif subnet.gateway_ip:
            data["gateway_ip"] = subnet.gateway_ip
        try:
            response = neutron.create_subnet({"subnet": data})
            backend_subnet = response["subnet"]
            subnet.backend_id = backend_subnet["id"]
            if backend_subnet.get("gateway_ip"):
                subnet.gateway_ip = backend_subnet["gateway_ip"]

            # Automatically create router for subnet
            if not subnet.tenant.skip_creation_of_default_router:
                self.connect_subnet(subnet)
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            subnet.save()

            event_logger.emit(
                "SubNet %s has been created in the backend." % subnet.name,
                event_type=EventType.OPENSTACK_SUBNET_CREATED,
                event_context={
                    "subnet": subnet,
                },
                scopes=[subnet, subnet.network],
            )

    @log_backend_action()
    @reraise_exceptions
    def update_subnet(self, subnet: models.SubNet):
        session = get_tenant_session(subnet.tenant)
        neutron = get_neutron_client(session)

        data = {
            "name": subnet.name,
            "dns_nameservers": subnet.dns_nameservers,
            "host_routes": subnet.host_routes,
        }

        # We should send gateway_ip only when it is changed, because
        # updating gateway_ip is prohibited when the ip is used.
        backend_subnet = neutron.show_subnet(subnet.backend_id)["subnet"]

        if backend_subnet["gateway_ip"] != subnet.gateway_ip:
            data["gateway_ip"] = subnet.gateway_ip

        if backend_subnet["cidr"] != subnet.cidr:
            data["cidr"] = subnet.cidr

        if backend_subnet["allocation_pools"] != subnet.allocation_pools:
            data["allocation_pools"] = subnet.allocation_pools

        neutron.update_subnet(subnet.backend_id, {"subnet": data})
        event_logger.emit(
            "SubNet %s has been updated" % subnet.name,
            event_type=EventType.OPENSTACK_SUBNET_UPDATED,
            event_context={
                "subnet": subnet,
            },
            scopes=[subnet, subnet.network],
        )

    def disconnect_subnet(self, subnet: models.SubNet):
        session = get_tenant_session(subnet.tenant)
        neutron = get_neutron_client(session)
        try:
            ports = neutron.list_ports(network_id=subnet.network.backend_id)["ports"]

            for port in ports:
                if port["device_owner"] not in VALID_ROUTER_INTERFACE_OWNERS:
                    continue
                neutron.remove_interface_router(
                    port["device_id"], {"subnet_id": subnet.backend_id}
                )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        else:
            subnet.is_connected = False
            subnet.save(update_fields=["is_connected"])

            event_logger.emit(
                "SubNet %s has been disconnected from network" % subnet.name,
                event_type=EventType.OPENSTACK_SUBNET_UPDATED,
                event_context={
                    "subnet": subnet,
                },
                scopes=[subnet, subnet.network],
            )

    def connect_subnet(self, subnet: models.SubNet):
        if subnet.tenant.skip_creation_of_default_router:
            logger.info(
                "Skipping router connection for subnet %s: tenant has skip_creation_of_default_router=True",
                subnet.name,
            )
            return

        self.connect_router(
            subnet.network.tenant,
            subnet.network.name,
            subnet.backend_id,
            network_id=subnet.network.backend_id,
        )
        subnet.is_connected = True
        subnet.save(update_fields=["is_connected"])

        event_logger.emit(
            "SubNet %s has been connected to network" % subnet.name,
            event_type=EventType.OPENSTACK_SUBNET_UPDATED,
            event_context={
                "subnet": subnet,
            },
            scopes=[subnet, subnet.network],
        )

    @log_backend_action()
    def delete_subnet(self, subnet: models.SubNet):
        session = get_tenant_session(subnet.tenant)
        neutron = get_neutron_client(session)
        try:
            self.disconnect_subnet(subnet)
            neutron.delete_subnet(subnet.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            subnet.decrease_backend_quotas_usage()
            event_logger.emit(
                "SubNet %s has been deleted" % subnet.name,
                event_type=EventType.OPENSTACK_SUBNET_DELETED,
                event_context={
                    "subnet": subnet,
                },
                scopes=[subnet, subnet.network],
            )

    def import_subnet(self, subnet: models.SubNet):
        session = get_tenant_session(subnet.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_subnet = neutron.show_subnet(subnet.backend_id)["subnet"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        is_connected = self.is_subnet_connected(
            subnet.tenant, backend_subnet["id"], backend_subnet["network_id"]
        )

        return self._backend_subnet_to_subnet(backend_subnet, is_connected=is_connected)

    def is_subnet_connected(
        self, tenant: models.Tenant, subnet_backend_id, subnet_network_backend_id
    ):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            ports = neutron.list_ports(network_id=subnet_network_backend_id)["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for port in ports:
            if port["device_owner"] not in VALID_ROUTER_INTERFACE_OWNERS:
                continue
            for fixed_ip in port["fixed_ips"]:
                if fixed_ip["subnet_id"] == subnet_backend_id:
                    return True
        return False

    @log_backend_action()
    def pull_subnet(self, subnet: models.SubNet):
        import_time = timezone.now()
        imported_subnet = self.import_subnet(subnet)

        subnet.refresh_from_db()
        if subnet.modified < import_time:
            modified = update_pulled_fields(
                subnet, imported_subnet, models.SubNet.get_backend_fields()
            )
            if modified:
                event_logger.emit(
                    "SubNet %s has been pulled from backend." % subnet.name,
                    event_type=EventType.OPENSTACK_SUBNET_PULLED,
                    event_context={
                        "subnet": subnet,
                    },
                    scopes=[subnet, subnet.network],
                )

    @log_backend_action("pull floating ip")
    def pull_floating_ip(self, floating_ip: models.FloatingIP):
        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_floating_ip = neutron.show_floatingip(floating_ip.backend_id)[
                "floatingip"
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        imported_floating_ip = self._backend_floating_ip_to_floating_ip(
            backend_floating_ip, floating_ip.tenant
        )
        update_pulled_fields(
            floating_ip, imported_floating_ip, models.FloatingIP.get_backend_fields()
        )

    @log_backend_action("delete floating ip")
    def delete_floating_ip(self, floating_ip: models.FloatingIP):
        self._delete_backend_floating_ip(
            floating_ip.tenant, floating_ip.backend_id, floating_ip.tenant.backend_id
        )
        floating_ip.decrease_backend_quotas_usage()

    def _delete_backend_floating_ip(
        self, tenant: models.Tenant, backend_id, tenant_backend_id
    ):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            logger.info(
                "Deleting floating IP %s from tenant %s", backend_id, tenant_backend_id
            )
            neutron.delete_floatingip(backend_id)
        except neutron_exceptions.NotFound:
            logger.debug(
                "Floating IP %s is already gone from tenant %s",
                backend_id,
                tenant_backend_id,
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action("update floating ip description")
    def update_floating_ip_description(
        self, floating_ip: models.FloatingIP, serialized_description
    ):
        description = serialized_description
        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        payload = {
            "description": description,
        }
        try:
            response_floating_ip = neutron.update_floatingip(
                floating_ip.backend_id, {"floatingip": payload}
            )["floatingip"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            floating_ip.runtime_state = response_floating_ip["status"]
            floating_ip.description = description
            floating_ip.save(update_fields=["runtime_state", "description"])

            event_logger.emit(
                f"The description of the floating IP [{floating_ip}] has been changed to [{description}].",
                event_type=EventType.OPENSTACK_FLOATING_IP_DESCRIPTION_UPDATED,
                event_context={
                    "floating_ip": floating_ip,
                },
                scopes=[floating_ip, floating_ip.tenant],
            )

    def _lookup_external_network_name(self, backend_id: str) -> str | None:
        """Best-effort cache lookup for the human-readable external-network name."""
        if not backend_id:
            return None
        net = (
            models.ExternalNetwork.objects.filter(
                settings=self.settings, backend_id=backend_id
            )
            .values_list("name", flat=True)
            .first()
        )
        return net or None

    @log_backend_action("create floating ip")
    def create_floating_ip(
        self, floating_ip: models.FloatingIP, serialized_router=None, **kwargs
    ):
        external_network_id = get_external_network_id(floating_ip.tenant)

        router = None
        if serialized_router:
            router = core_utils.deserialize_instance(serialized_router)

        # If external_network_id from settings but not on tenant, attempt recovery
        if external_network_id and not floating_ip.tenant.external_network_id:
            logger.info(
                "Attempting to recover external network for tenant %s before floating IP creation",
                floating_ip.tenant,
            )
            try:
                self.detect_external_network(floating_ip.tenant, router=router)
                floating_ip.tenant.refresh_from_db()
                # Re-check after recovery attempt
                external_network_id = get_external_network_id(floating_ip.tenant)
            except Exception as e:
                logger.warning(
                    "Failed to recover external network for tenant %s: %s. Proceeding with settings value.",
                    floating_ip.tenant,
                    e,
                )

        if not external_network_id:
            raise OpenStackBackendError(
                "Cannot create floating IP: external network ID is not defined for tenant."
            )

        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_floating_ip = neutron.create_floatingip(
                {
                    "floatingip": {
                        "floating_network_id": external_network_id,
                        "tenant_id": floating_ip.tenant.backend_id,
                    }
                }
            )["floatingip"]
        except (
            neutron_exceptions.IpAddressGenerationFailureClient,
            neutron_exceptions.ExternalIpAddressExhaustedClient,
        ) as e:
            # The external network's allocation pool is exhausted. Bare
            # 'IpAddressGenerationFailureClient: No more IP addresses
            # available' is unactionable; tell the caller exactly which
            # pool is full and who to escalate to.
            external_network_name = self._lookup_external_network_name(
                external_network_id
            )
            logger.warning(
                "Floating IP allocation failed for tenant %s on external "
                "network %s (%s): pool exhausted. Original error: %s",
                floating_ip.tenant.backend_id,
                external_network_name or "?",
                external_network_id,
                e,
            )
            floating_ip.runtime_state = "ERRED"
            floating_ip.save()
            raise OpenStackBackendError(
                f"Cannot allocate floating IP: the external network "
                f"'{external_network_name or external_network_id}' has no "
                f"free addresses in its allocation pool. Contact your "
                f"cloud administrator to extend the pool."
            ) from e
        except neutron_exceptions.NeutronClientException as e:
            floating_ip.runtime_state = "ERRED"
            floating_ip.save()
            raise OpenStackBackendError(e)
        else:
            floating_ip.runtime_state = backend_floating_ip["status"]
            floating_ip.address = backend_floating_ip["floating_ip_address"]
            floating_ip.name = backend_floating_ip["floating_ip_address"]
            floating_ip.backend_id = backend_floating_ip["id"]
            floating_ip.backend_network_id = backend_floating_ip["floating_network_id"]
            floating_ip.save()

    @log_backend_action()
    def pull_floating_ip_runtime_state(self, floating_ip: models.FloatingIP):
        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_floating_ip = neutron.show_floatingip(floating_ip.backend_id)[
                "floatingip"
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            floating_ip.runtime_state = backend_floating_ip["status"]
            floating_ip.save()

    @log_backend_action()
    def connect_tenant_to_external_network(
        self, tenant: models.Tenant, external_network_id
    ):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        logger.debug(
            'About to connect tenant to external network "%s" (PK: %s)',
            tenant.name,
            tenant.pk,
        )

        try:
            # check if the network actually exists
            response = neutron.show_network(external_network_id)
        except neutron_exceptions.NeutronClientException as e:
            logger.exception(
                "External network %s does not exist. Stale data in database?",
                external_network_id,
            )
            raise OpenStackBackendError(e)

        network_name = response["network"]["name"]
        subnet_id = response["network"]["subnets"][0]
        self.connect_router(
            tenant,
            network_name,
            subnet_id,
            external=True,
            network_id=response["network"]["id"],
        )

        tenant.external_network_id = external_network_id
        # Also set the FK if an ExternalNetwork record exists
        ext_net = models.ExternalNetwork.objects.filter(
            settings=tenant.service_settings,
            backend_id=external_network_id,
        ).first()
        if ext_net:
            tenant.external_network_ref = ext_net
        tenant.save()

        logger.info(
            "Router between external network %s and tenant %s was successfully created",
            external_network_id,
            tenant.backend_id,
        )

        return external_network_id

    def _get_router(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)["routers"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        # If any router in Tenant exists, use it
        return routers[0] if routers else None

    def create_router(self, router: models.Router):
        backend_router = self._create_router(router.tenant, router.name)
        router.backend_id = backend_router["id"]
        router.save(update_fields=["backend_id"])

    def _create_router(self, tenant: models.Tenant, router_name):
        neutron = get_neutron_client(self.admin_session)
        create_ha_routers = bool(
            tenant.service_settings.options.get("create_ha_routers")
        )
        options = {
            "router": {
                "name": router_name,
                "tenant_id": tenant.backend_id,
            }
        }
        if create_ha_routers:
            options["router"]["ha"] = create_ha_routers

        try:
            router = neutron.create_router(options)["router"]
            logger.info("Router %s has been created in the backend.", router["name"])
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        return router

    def _connect_network_to_router(
        self, tenant: models.Tenant, router, external, network_id=None, subnet_id=None
    ):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            if external:
                if (
                    not router.get("external_gateway_info")
                    or router["external_gateway_info"].get("network_id") != network_id
                ):
                    backend_router = neutron.add_gateway_router(
                        router["id"], {"network_id": network_id}
                    )["router"]
                    # Neutron allocates the gateway address from a subnet of the
                    # external network, but an external network with no subnets
                    # is legal: Subnet.network_has_no_subnet treats it as "not an
                    # error" and _create_router_gw_port only logs "No IPs
                    # available for external network", returning the router with
                    # an empty external_fixed_ips. Indexing [0] to build a log
                    # message turned that supported case into an IndexError that
                    # failed the whole tenant provisioning.
                    external_fixed_ips = (
                        backend_router.get("external_gateway_info") or {}
                    ).get("external_fixed_ips") or []
                    if external_fixed_ips:
                        external_ip_info = external_fixed_ips[0]
                        logger.info(
                            "External network %s has been connected to the router %s with external IP %s within subnet %s.",
                            network_id,
                            router["name"],
                            external_ip_info["ip_address"],
                            external_ip_info["subnet_id"],
                        )
                    else:
                        logger.warning(
                            "External network %s has been connected to the router %s, "
                            "but no external IP was allocated: the network has no subnet "
                            "to allocate from.",
                            network_id,
                            router["name"],
                        )
                else:
                    logger.info(
                        "External network %s is already connected to router %s.",
                        network_id,
                        router["name"],
                    )
            else:
                subnet = neutron.show_subnet(subnet_id)["subnet"]
                # Subnet for router interface must have a gateway IP.
                if not subnet["gateway_ip"]:
                    return
                ports = neutron.list_ports(
                    device_id=router["id"],
                    tenant_id=tenant.backend_id,
                    network_id=network_id,
                )["ports"]
                if not ports:
                    neutron.add_interface_router(router["id"], {"subnet_id": subnet_id})
                    logger.info(
                        "Internal subnet %s was connected to the router %s.",
                        subnet_id,
                        router["name"],
                    )
                else:
                    logger.info(
                        "Internal subnet %s is already connected to the router %s.",
                        subnet_id,
                        router["name"],
                    )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    def connect_router(
        self,
        tenant: models.Tenant,
        network_name,
        subnet_id,
        external=False,
        network_id=None,
    ):
        if tenant.skip_creation_of_default_router:
            logger.info(
                "Skipping router connection for tenant %s: skip_creation_of_default_router=True",
                tenant.name,
            )
            return None

        router_name = f"{network_name}-router"
        router = self._get_router(tenant) or self._create_router(tenant, router_name)
        self._connect_network_to_router(tenant, router, external, network_id, subnet_id)

        return router["id"]

    @log_backend_action()
    def update_tenant(self, tenant: models.Tenant):
        session = get_keystone_session(tenant.service_settings)
        keystone = get_keystone_client(session)
        try:
            keystone.projects.update(
                tenant.backend_id, name=tenant.name, description=tenant.description
            )
        except keystone_exceptions.NotFound as e:
            logger.error("Tenant with id %s does not exist", tenant.backend_id)
            raise OpenStackBackendError(e)

    def get_allocation_candidates(self, resources, required=None, limit=None):
        """Pre-flight scheduler check via Placement.

        Returns the raw Placement `/allocation_candidates` response so the
        caller can decide what to project (count, per-RP summaries, etc.).
        See PlacementClient.list_allocation_candidates for the parameter
        contract.
        """
        placement = get_placement_client(self.admin_session)
        return placement.list_allocation_candidates(
            resources=resources, required=required, limit=limit
        )

    def get_instance_placement_allocations(self, instance):
        """Return Placement allocations for a specific instance, broken down
        by resource provider with names resolved.

        Each Nova server's UUID is its Placement consumer UUID, so the join
        is trivial. Useful for "what is this instance actually consuming?" —
        especially for VGPU/PCI/specialty resources that the flavor alone
        does not describe.

        Returns a list shaped like::

            [{
              "resource_provider_uuid": "...",
              "resource_provider_name": "compute01",
              "resources": {"VCPU": 1, "MEMORY_MB": 1024, "DISK_GB": 10},
            }, ...]

        Empty list when Placement has no record (transient state right after
        create, or instance never scheduled).
        """
        placement = get_placement_client(self.admin_session)
        allocations = placement.get_allocations(instance.backend_id)
        if not allocations:
            return []
        rp_names = {
            rp.get("uuid"): rp.get("name", "")
            for rp in placement.list_resource_providers()
        }
        return [
            {
                "resource_provider_uuid": rp_uuid,
                "resource_provider_name": rp_names.get(rp_uuid, ""),
                "resources": (record or {}).get("resources", {}),
            }
            for rp_uuid, record in allocations.items()
        ]

    def pull_service_settings_quotas(self):
        # Aggregate cluster-wide capacity from Placement, summing the same
        # effective totals (allocation_ratio applied, reserved subtracted) used
        # by pull_hypervisors. This replaces nova.hypervisor_stats.statistics()
        # which is removed at microversion 2.88.
        placement_capacity, _ = self._collect_placement_data()

        def aggregate(resource_class):
            total = used = 0
            for per_class in placement_capacity.values():
                inv = per_class.get(resource_class)
                if not inv:
                    continue
                total += self._effective_total(inv)
                used += inv.get("used", 0)
            return total, used

        total_vcpu, used_vcpu = aggregate("VCPU")
        total_ram, used_ram = aggregate("MEMORY_MB")

        self.settings.set_quota_limit("openstack_vcpu", total_vcpu)
        self.settings.set_quota_usage("openstack_vcpu", used_vcpu)

        self.settings.set_quota_limit("openstack_ram", total_ram)
        self.settings.set_quota_usage("openstack_ram", used_ram)

        self.settings.set_quota_usage("openstack_storage", self.get_storage_usage())

    def get_storage_usage(self):
        cinder = get_cinder_client(self.admin_session)

        try:
            volumes = cinder.volumes.list()
            snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        storage = sum(self.gb2mb(v.size) for v in volumes + snapshots)
        return storage

    @log_backend_action()
    def create_port(self, port: models.Port):
        # Use admin session for all port creation to ensure consistent behavior
        # and avoid authorization issues with shared networks
        session = self.admin_session
        neutron = get_neutron_client(session)
        network = port.network

        port_payload = {
            "name": port.name,
            "description": port.description,
            "network_id": network.backend_id,
            "tenant_id": port.tenant.backend_id,
            "port_security_enabled": port.port_security_enabled,
        }

        logger.info(
            "Port creation payload - network_id: %s, tenant_id: %s, network_tenant: %s, session_type: admin",
            network.backend_id,
            port.tenant.backend_id,
            port.network.tenant.backend_id,
        )
        if port.fixed_ips:
            port_payload["fixed_ips"] = port.fixed_ips

        if port.mac_address:
            port_payload["mac_address"] = port.mac_address

        try:
            port_response = neutron.create_port({"port": port_payload})["port"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            port.mac_address = port_response["mac_address"]
            port.backend_id = port_response["id"]
            port.fixed_ips = port_response["fixed_ips"]
            port.admin_state_up = port_response["admin_state_up"]
            port.port_security_enabled = port_response["port_security_enabled"]
            port.device_owner = port_response["device_owner"]
            port.status = port_response["status"]
            port.save()

            event_logger.emit(
                f"Port [{port}] has been created in the backend for network [{network}].",
                event_type=EventType.OPENSTACK_PORT_CREATED,
                event_context={"port": port},
                scopes=[
                    port,
                    port.network,
                ],
            )

            return port

    @log_backend_action()
    def delete_port(self, port: models.Port):
        if not port.backend_id:
            logger.info("Skipping port deletion: port %s has no backend_id", port.uuid)
            return

        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)

        try:
            neutron.delete_port(port.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            event_logger.emit(
                f"Port [{port}] has been deleted from network [{port.network}].",
                event_type=EventType.OPENSTACK_PORT_DELETED,
                event_context={"port": port},
                scopes=[
                    port,
                    port.network,
                ],
            )

    @log_backend_action()
    def attach_floating_ip_to_port(
        self, floating_ip: models.FloatingIP, serialized_port
    ):
        port: models.Port = core_utils.deserialize_instance(serialized_port)
        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        payload = {
            "port_id": port.backend_id,
        }
        try:
            response_floating_ip = neutron.update_floatingip(
                floating_ip.backend_id, {"floatingip": payload}
            )["floatingip"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            floating_ip.runtime_state = response_floating_ip["status"]
            floating_ip.address = response_floating_ip["fixed_ip_address"]
            floating_ip.port = port
            floating_ip.save(update_fields=["address", "runtime_state", "port"])

            event_logger.emit(
                f"Floating IP [{floating_ip}] has been attached to port [{port}].",
                event_type=EventType.OPENSTACK_FLOATING_IP_ATTACHED,
                event_context={
                    "floating_ip": floating_ip,
                    "port": port,
                },
                scopes=[floating_ip, floating_ip.tenant, port],
            )

    @log_backend_action()
    def detach_floating_ip_from_port(self, floating_ip: models.FloatingIP):
        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        payload = {
            "port_id": None,
        }
        try:
            response_floating_ip = neutron.update_floatingip(
                floating_ip.backend_id, {"floatingip": payload}
            )["floatingip"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            port = floating_ip.port
            floating_ip.runtime_state = response_floating_ip["status"]
            floating_ip.address = None
            floating_ip.port = None
            floating_ip.save(update_fields=["address", "runtime_state", "port"])

            event_logger.emit(
                f"Floating IP {floating_ip} has been detached from port {port}.",
                event_type=EventType.OPENSTACK_FLOATING_IP_DETACHED,
                event_context={
                    "floating_ip": floating_ip,
                    "port": port,
                },
                scopes=[floating_ip, floating_ip.tenant, port],
            )

    def _log_server_group_imported(self, server_group: models.ServerGroup):
        event_logger.emit(
            "Server group %s has been imported to local cache." % server_group.name,
            event_type=EventType.OPENSTACK_SERVER_GROUP_IMPORTED,
            event_context={"server_group": server_group},
            scopes=[server_group, server_group.tenant],
        )

    def _log_server_group_pulled(self, server_group: models.ServerGroup):
        event_logger.emit(
            "Server group %s has been pulled from backend." % server_group.name,
            event_type=EventType.OPENSTACK_SERVER_GROUP_PULLED,
            event_context={"server_group": server_group},
            scopes=[server_group, server_group.tenant],
        )

    def _log_server_group_created(self, server_group: models.ServerGroup):
        event_logger.emit(
            'Server group "%s" has been created in the backend.' % server_group.name,
            event_type=EventType.OPENSTACK_SERVER_GROUP_CREATED,
            event_context={"server_group": server_group},
            scopes=[server_group, server_group.tenant],
        )

    def _backend_server_group_to_server_group(self, backend_server_group, **kwargs):
        # Nova microversion 2.64 replaced the `policies` list with a single
        # `policy` string on server-group responses; we pin to 2.87 in
        # get_nova_client, so only `policy` is present.
        server_group = models.ServerGroup(
            name=backend_server_group.name,
            policy=backend_server_group.policy,
            backend_id=backend_server_group.id,
            state=CoreStates.OK,
        )

        for field, value in kwargs.items():
            setattr(server_group, field, value)

        return server_group

    def _update_tenant_server_groups(
        self, tenant: models.Tenant, backend_server_groups
    ):
        for backend_server_group in backend_server_groups:
            imported_server_group = self._backend_server_group_to_server_group(
                backend_server_group,
                tenant=tenant,
                service_settings=tenant.service_settings,
                project=tenant.project,
            )
            try:
                server_group = tenant.server_groups.get(
                    backend_id=imported_server_group.backend_id
                )
            except models.ServerGroup.DoesNotExist:
                imported_server_group.save()
                server_group = imported_server_group
                self._log_server_group_imported(server_group)
            else:
                if server_group.state not in (
                    CoreStates.OK,
                    CoreStates.ERRED,
                ):
                    logger.info(
                        "Skipping pulling of OpenStack server group because it is "
                        "not in the stable state. Group ID: %s",
                        server_group.id,
                    )
                    continue
                modified = update_pulled_fields(
                    server_group,
                    imported_server_group,
                    models.ServerGroup.get_backend_fields(),
                )
                handle_resource_update_success(server_group)

                if modified:
                    self._log_server_group_pulled(server_group)

    def _remove_stale_server_groups(self, tenants, backend_server_groups):
        remote_ids = {ip.id for ip in backend_server_groups}
        stale_groups = models.ServerGroup.objects.filter(
            tenant__in=tenants,
            state__in=[
                CoreStates.OK,
                CoreStates.ERRED,
            ],
        ).exclude(backend_id__in=remote_ids)
        for server_group in stale_groups:
            event_logger.emit(
                "Server group %s has been cleaned from cache." % server_group.name,
                event_type=EventType.OPENSTACK_SERVER_GROUP_CLEANED,
                event_context={
                    "server_group": server_group,
                },
                scopes=[server_group, server_group.tenant],
            )
        stale_groups.delete()

    def pull_server_group(self, local_server_group: models.ServerGroup):
        session = get_tenant_session(local_server_group.tenant)
        nova = get_nova_client(session)
        try:
            remote_server_group = nova.server_groups.get(local_server_group.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        imported_server_group = self._backend_server_group_to_server_group(
            remote_server_group,
            tenant=local_server_group.tenant,
            service_settings=local_server_group.tenant.service_settings,
            project=local_server_group.tenant.project,
        )

        modified = update_pulled_fields(
            local_server_group,
            imported_server_group,
            models.ServerGroup.get_backend_fields(),
        )

        if modified:
            self._log_server_group_pulled(local_server_group)

    @log_backend_action("pull server groups for tenant")
    def pull_tenant_server_groups(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)
        try:
            backend_server_groups = nova.server_groups.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            self._update_tenant_server_groups(tenant, backend_server_groups)
            self._remove_stale_server_groups([tenant], backend_server_groups)

    def parse_backend_port(self, remote_port, **kwargs):
        fixed_ips = remote_port["fixed_ips"]

        logger.debug(
            "Parsing backend port %s: fixed_ips=%s, mac_address=%s, device_id=%s",
            remote_port["id"],
            fixed_ips,
            remote_port["mac_address"],
            remote_port.get("device_id"),
        )

        local_port = models.Port(
            backend_id=remote_port["id"],
            mac_address=remote_port["mac_address"],
            fixed_ips=fixed_ips,
            allowed_address_pairs=remote_port.get("allowed_address_pairs", []),
            admin_state_up=remote_port["admin_state_up"],
            name=remote_port["name"],
            description=remote_port["description"],
        )

        for field, value in kwargs.items():
            setattr(local_port, field, value)

        if "instance" not in kwargs:
            local_port._instance_backend_id = remote_port["device_id"]
        if "subnet" not in kwargs:
            if fixed_ips:
                local_port._subnet_backend_id = fixed_ips[0]["subnet_id"]

        local_port._device_owner = remote_port["device_owner"]

        return local_port

    def pull_tenant_volumes(self, tenant: models.Tenant):
        backend_volumes = self.get_volumes(tenant)
        volumes = models.Volume.objects.filter(
            tenant=tenant,
            state__in=[CoreStates.OK, CoreStates.ERRED],
        )
        backend_volumes_map = {
            backend_volume.backend_id: backend_volume
            for backend_volume in backend_volumes
        }
        for volume in volumes:
            try:
                backend_volume = backend_volumes_map[volume.backend_id]
            except KeyError:
                handle_resource_not_found(volume)
            else:
                update_pulled_fields(
                    volume,
                    backend_volume,
                    models.Volume.get_backend_fields(),
                )
                handle_resource_update_success(volume)

    def pull_tenant_snapshots(self, tenant: models.Tenant):
        backend_snapshots = self.get_snapshots(tenant)
        snapshots = models.Snapshot.objects.filter(
            tenant=tenant,
            state__in=[
                CoreStates.OK,
                CoreStates.ERRED,
            ],
        )
        backend_snapshots_map = {
            backend_snapshot.backend_id: backend_snapshot
            for backend_snapshot in backend_snapshots
        }
        for snapshot in snapshots:
            try:
                backend_snapshot = backend_snapshots_map[snapshot.backend_id]
            except KeyError:
                handle_resource_not_found(snapshot)
            else:
                update_pulled_fields(
                    snapshot,
                    backend_snapshot,
                    models.Snapshot.get_backend_fields(),
                )
                handle_resource_update_success(snapshot)

    def pull_tenant_instances(self, tenant: models.Tenant):
        instances = models.Instance.objects.filter(
            tenant=tenant,
            state__in=[CoreStates.OK, CoreStates.ERRED],
        )
        for instance in instances:
            try:
                # Use pull_instance which has all the enhanced logic including image detection
                self.pull_instance(instance)
                # XXX: can be optimized after https://goo.gl/BZKo8Y will be resolved.
                self.pull_instance_security_groups(instance)
                handle_resource_update_success(instance)
            except nova_exceptions.NotFound:
                handle_resource_not_found(instance)
            except nova_exceptions.ClientException:
                # Log the error but continue with other instances
                handle_resource_update_success(instance)

    def pull_instance_server_group(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        server_id = instance.backend_id
        try:
            backend_server_groups = nova.server_groups.list()
            filtered_backend_server_groups = [
                group
                for group in backend_server_groups
                if server_id in group._info["members"]
            ]
        except nova_exceptions.NotFound:
            return True
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        try:
            server_group_backend_id = filtered_backend_server_groups[0].id
        except IndexError:
            instance.server_group = None
        else:
            try:
                server_group = models.ServerGroup.objects.get(
                    tenant=instance.tenant, backend_id=server_group_backend_id
                )
            except models.ServerGroup.DoesNotExist:
                logger.exception(
                    f"Server group with id {server_group_backend_id} does not exist in database. "
                    f"Server ID: {server_id}"
                )
            else:
                instance.server_group = server_group

    @log_backend_action()
    def create_volume(self, volume: models.Volume):
        kwargs = {
            "size": self.mb2gb(volume.size),
            "name": volume.name,
            "description": volume.description,
        }

        if volume.source_snapshot:
            kwargs["snapshot_id"] = volume.source_snapshot.backend_id

        tenant = volume.tenant

        # there is an issue in RHOS13 that doesn't allow to restore a snapshot to a volume if also a volume type ID is provided
        # a workaround is to avoid setting volume type in this case at all
        if not volume.source_snapshot:
            if volume.type:
                kwargs["volume_type"] = volume.type.backend_id
            else:
                volume_type_name = tenant and tenant.default_volume_type_name
                if volume_type_name:
                    try:
                        volume_type = models.VolumeType.objects.get(
                            name=volume_type_name,
                            settings=tenant.service_settings,
                        )
                        volume.type = volume_type
                        kwargs["volume_type"] = volume_type.backend_id
                    except models.VolumeType.DoesNotExist:
                        logger.error(
                            f"Volume type is not set as volume type with name {volume_type_name} is not found. Settings UUID: {volume.service_settings.uuid.hex}"
                        )
                    except models.VolumeType.MultipleObjectsReturned:
                        logger.error(
                            f"Volume type is not set as multiple volume types with name {volume_type_name} are found."
                            f"Service settings UUID: {volume.service_settings.uuid.hex}"
                        )

        if volume.availability_zone:
            kwargs["availability_zone"] = volume.availability_zone.name
        else:
            volume_availability_zone_name = (
                tenant
                and tenant.service_settings.options.get("volume_availability_zone_name")
            )

            if volume_availability_zone_name:
                try:
                    volume_availability_zone = (
                        models.VolumeAvailabilityZone.objects.get(
                            name=volume_availability_zone_name,
                            settings=volume.service_settings,
                        )
                    )
                    volume.availability_zone = volume_availability_zone
                    kwargs["availability_zone"] = volume_availability_zone.name
                except models.VolumeAvailabilityZone.DoesNotExist:
                    logger.error(
                        f"Volume availability zone with name {volume_availability_zone_name} is not found. Settings UUID: {volume.service_settings.uuid.hex}"
                    )

        if volume.image:
            kwargs["imageRef"] = volume.image.backend_id
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            logger.info("Creating volume with parameters: %s", kwargs)
            backend_volume = cinder.volumes.create(**kwargs)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.backend_id = backend_volume.id
        if hasattr(backend_volume, "volume_image_metadata"):
            volume.image_metadata = backend_volume.volume_image_metadata
        # Cinder reports bootable="false" right after create for image-backed
        # volumes until the image copy finishes, so only ever upgrade the flag
        # here. Downgrading would clear the bootable flag that the serializer
        # set on a system volume, making create_instance fail its
        # `volumes.get(bootable=True)` guard (PUHURI-PORTALS-T2B).
        volume.bootable = volume.bootable or backend_volume.bootable == "true"
        volume.runtime_state = backend_volume.status
        volume.save()
        return volume

    @log_backend_action()
    def update_volume(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volumes.update(
                volume.backend_id, name=volume.name, description=volume.description
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_volume(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volumes.delete(volume.backend_id)
        except cinder_exceptions.NotFound:
            logger.info(
                "OpenStack volume %s has been already deleted", volume.backend_id
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.decrease_backend_quotas_usage()

    @log_backend_action()
    def attach_volume(self, volume: models.Volume, instance_uuid, device=None):
        instance = models.Instance.objects.get(uuid=instance_uuid)
        session = get_tenant_session(volume.tenant)
        nova = get_nova_client(session)
        try:
            nova.volumes.create_server_volume(
                instance.backend_id,
                volume.backend_id,
                device=None if device == "" else device,
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            volume.instance = instance
            volume.device = device
            volume.save(update_fields=["instance", "device"])

    @log_backend_action()
    def detach_volume(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        nova = get_nova_client(session)
        try:
            nova.volumes.delete_server_volume(
                volume.instance.backend_id, volume.backend_id
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            volume.instance = None
            volume.device = ""
            volume.save(update_fields=["instance", "device"])

    @log_backend_action()
    def extend_volume(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(
            session,
            api_version="3.51"
            if volume.service_settings.options.get(
                "live_resize_of_volumes_enabled", False
            )
            else None,
        )
        try:
            cinder.volumes.extend(volume.backend_id, self.mb2gb(volume.size))
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def import_volume(self, tenant: models.Tenant, backend_id, project=None, save=True):
        """Restore Waldur volume instance based on backend data."""
        try:
            session = get_tenant_session(tenant)
            cinder = get_cinder_client(session)
        except OpenStackAuthorizationFailed as e:
            logger.error(
                "Failed to authenticate with OpenStack for tenant %s: %s",
                tenant.uuid,
                e,
            )
            raise
        try:
            backend_volume = cinder.volumes.get(backend_id)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume = self._backend_volume_to_volume(tenant, backend_volume)
        volume.service_settings = tenant.service_settings
        volume.tenant = tenant
        volume.project = project
        volume.device = (
            volume.device or ""
        )  # In case if device of an imported volume is null
        if save:
            volume.save()

        return volume

    def _backend_volume_to_volume(self, tenant: models.Tenant, backend_volume):
        volume_type = None
        availability_zone = None

        try:
            if backend_volume.volume_type:
                volume_type = models.VolumeType.objects.get(
                    name=backend_volume.volume_type,
                    settings=tenant.service_settings,
                )
        except models.VolumeType.DoesNotExist:
            pass
        except models.VolumeType.MultipleObjectsReturned:
            logger.error(
                "Volume type is not set as multiple volume types with name %s are found."
                "Service settings UUID: %s",
                (backend_volume.volume_type, tenant.service_settings.uuid.hex),
            )

        try:
            backend_volume_availability_zone = getattr(
                backend_volume, "availability_zone", None
            )
            if backend_volume_availability_zone:
                availability_zone = models.VolumeAvailabilityZone.objects.get(
                    name=backend_volume_availability_zone, settings=self.settings
                )
        except models.VolumeAvailabilityZone.DoesNotExist:
            pass

        volume = models.Volume(
            name=backend_volume.name,
            description=backend_volume.description or "",
            size=self.gb2mb(backend_volume.size),
            metadata=backend_volume.metadata,
            backend_id=backend_volume.id,
            type=volume_type,
            bootable=backend_volume.bootable == "true",
            runtime_state=backend_volume.status,
            state=CoreStates.OK,
            availability_zone=availability_zone,
        )
        if getattr(backend_volume, "volume_image_metadata", False):
            volume.image_metadata = backend_volume.volume_image_metadata
            try:
                image_id = volume.image_metadata.get("image_id")
                if image_id:
                    volume.image = models.Image.objects.get(
                        settings=tenant.service_settings, backend_id=image_id
                    )
            except models.Image.DoesNotExist:
                pass

            volume.image_name = volume.image_metadata.get("image_name", "")

        # In our setup volume could be attached only to one instance.
        if getattr(backend_volume, "attachments", False):
            if "device" in backend_volume.attachments[0]:
                volume.device = backend_volume.attachments[0]["device"] or ""

            if "server_id" in backend_volume.attachments[0]:
                volume.instance = models.Instance.objects.filter(
                    tenant=tenant,
                    backend_id=backend_volume.attachments[0]["server_id"],
                ).first()
        return volume

    def get_volumes(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)
        try:
            backend_volumes = cinder.volumes.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        return [
            self._backend_volume_to_volume(tenant, backend_volume)
            for backend_volume in backend_volumes
        ]

    @log_backend_action()
    def remove_bootable_flag(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
            cinder.volumes.set_bootable(backend_volume, False)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.bootable = False
        volume.save(update_fields=["bootable"])

    @log_backend_action()
    def toggle_bootable_flag(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
            cinder.volumes.set_bootable(backend_volume, volume.bootable)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.save(update_fields=["bootable"])

    @log_backend_action()
    def pull_volume(self, volume: models.Volume, update_fields=None):
        import_time = timezone.now()
        imported_volume = self.import_volume(
            volume.tenant, volume.backend_id, save=False
        )

        volume.refresh_from_db()
        if volume.modified < import_time:
            if not update_fields:
                update_fields = models.Volume.get_backend_fields()

            update_pulled_fields(volume, imported_volume, update_fields)

        resource_pulled.send(sender=volume.__class__, instance=volume)

    @log_backend_action()
    def pull_volume_runtime_state(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
        except cinder_exceptions.NotFound:
            volume.runtime_state = "deleted"
            volume.save(update_fields=["runtime_state"])
            return
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            if backend_volume.status != volume.runtime_state:
                volume.runtime_state = backend_volume.status
                volume.save(update_fields=["runtime_state"])

    @log_backend_action("check is volume deleted")
    def is_volume_deleted(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volumes.get(volume.backend_id)
            return False
        except cinder_exceptions.NotFound:
            return True
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def retype_volume(self, volume: models.Volume):
        session = get_tenant_session(volume.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volumes.retype(volume.backend_id, volume.type.name, "on-demand")
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_snapshot(self, snapshot: models.Snapshot, force=True):
        kwargs = {
            "name": snapshot.name,
            "description": snapshot.description,
            "force": force,
        }
        session = get_tenant_session(snapshot.tenant)
        cinder = get_cinder_client(session)
        try:
            backend_snapshot = cinder.volume_snapshots.create(
                snapshot.source_volume.backend_id, **kwargs
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        snapshot.backend_id = backend_snapshot.id
        snapshot.runtime_state = backend_snapshot.status
        snapshot.size = self.gb2mb(backend_snapshot.size)
        snapshot.save()
        return snapshot

    def import_snapshot(self, snapshot: models.Snapshot, project=None, save=True):
        """Restore Waldur Snapshot instance based on backend data."""
        session = get_tenant_session(snapshot.tenant)
        cinder = get_cinder_client(session)
        try:
            backend_snapshot = cinder.volume_snapshots.get(snapshot.backend_id)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        snapshot = self._backend_snapshot_to_snapshot(snapshot.tenant, backend_snapshot)
        snapshot.service_settings = snapshot.service_settings
        snapshot.tenant = snapshot.tenant
        snapshot.project = project
        if save:
            snapshot.save()
        return snapshot

    def _backend_snapshot_to_snapshot(self, tenant: models.Tenant, backend_snapshot):
        snapshot = models.Snapshot(
            name=backend_snapshot.name,
            description=backend_snapshot.description or "",
            size=self.gb2mb(backend_snapshot.size),
            metadata=backend_snapshot.metadata,
            backend_id=backend_snapshot.id,
            runtime_state=backend_snapshot.status,
            state=CoreStates.OK,
        )
        if hasattr(backend_snapshot, "volume_id"):
            snapshot.source_volume = models.Volume.objects.filter(
                tenant=tenant,
                backend_id=backend_snapshot.volume_id,
            ).first()
        return snapshot

    def get_snapshots(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)
        try:
            backend_snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        return [
            self._backend_snapshot_to_snapshot(tenant, backend_snapshot)
            for backend_snapshot in backend_snapshots
        ]

    @log_backend_action()
    def pull_snapshot(self, snapshot: models.Snapshot, update_fields=None):
        import_time = timezone.now()
        imported_snapshot = self.import_snapshot(snapshot.backend_id, save=False)

        snapshot.refresh_from_db()
        if snapshot.modified < import_time:
            if update_fields is None:
                update_fields = models.Snapshot.get_backend_fields()
            update_pulled_fields(snapshot, imported_snapshot, update_fields)

    @log_backend_action()
    def pull_snapshot_runtime_state(self, snapshot: models.Snapshot):
        session = get_tenant_session(snapshot.tenant)
        cinder = get_cinder_client(session)
        try:
            backend_snapshot = cinder.volume_snapshots.get(snapshot.backend_id)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        if backend_snapshot.status != snapshot.runtime_state:
            snapshot.runtime_state = backend_snapshot.status
            snapshot.save(update_fields=["runtime_state"])
        return snapshot

    @log_backend_action()
    def delete_snapshot(self, snapshot: models.Snapshot):
        session = get_tenant_session(snapshot.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volume_snapshots.delete(snapshot.backend_id)
        except cinder_exceptions.NotFound:
            logger.info(
                "Snapshot with ID %s is missing from OpenStack" % snapshot.backend_id
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        snapshot.decrease_backend_quotas_usage()

    @log_backend_action()
    def update_snapshot(self, snapshot: models.Snapshot):
        session = get_tenant_session(snapshot.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volume_snapshots.update(
                snapshot.backend_id,
                name=snapshot.name,
                description=snapshot.description,
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action("check is snapshot deleted")
    def is_snapshot_deleted(self, snapshot: models.Snapshot):
        session = get_tenant_session(snapshot.tenant)
        cinder = get_cinder_client(session)
        try:
            cinder.volume_snapshots.get(snapshot.backend_id)
            return False
        except cinder_exceptions.NotFound:
            return True
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def is_volume_availability_zone_supported(self):
        cinder = get_cinder_client(self.admin_session)
        return "AvailabilityZones" in [
            e.name for e in list_extensions.ListExtManager(cinder).show_all()
        ]

    def _create_port_in_external_network(self, tenant: models.Tenant, security_groups):
        external_network_id = tenant.external_network_id
        if not external_network_id:
            raise OpenStackBackendError(
                "Cannot create an instance directly attached to external network without a defined external_network_id."
            )

        logger.debug(
            "About to create network port in external network. Network ID: %s.",
            external_network_id,
        )
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            port = {
                "network_id": external_network_id,
                "tenant_id": tenant.backend_id,  # admin only functionality
                "security_groups": security_groups,
            }
            backend_external_port = neutron.create_port({"port": port})["port"]
            return backend_external_port
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_instance(
        self,
        instance: models.Instance,
        backend_flavor_id=None,
        public_key=None,
        server_group=None,
    ):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)

        try:
            try:
                backend_flavor = nova.flavors.get(backend_flavor_id)
            except nova_exceptions.NotFound:
                raise OpenStackBackendError(
                    "Flavor with backend ID '%s' was not found in OpenStack. "
                    "It may have been removed or recreated; refresh the "
                    "offering's flavors to update the cached references."
                    % backend_flavor_id
                )

            # instance key name and fingerprint_md5 are optional
            # it is assumed that if public_key is specified, then
            # key_name and key_fingerprint have valid values
            if public_key:
                backend_public_key = self._get_or_create_ssh_key(
                    instance.tenant,
                    instance.key_name,
                    instance.key_fingerprint,
                    public_key,
                )
            else:
                backend_public_key = None

            try:
                instance.volumes.get(bootable=True)
            except models.Volume.DoesNotExist:
                raise OpenStackBackendError(
                    "Current installation cannot create instance without a system volume."
                )

            nics = []

            logger.info(
                "Processing %d ports for instance %s creation",
                instance.ports.count(),
                instance.name,
            )

            ports_without_backend_id = []
            for port in instance.ports.all():
                if not port.backend_id:
                    ports_without_backend_id.append(port)
                    continue
                nics.append({"port-id": port.backend_id})

            if (
                settings.WALDUR_OPENSTACK["ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION"]
                and instance.connect_directly_to_external_network
            ):
                security_groups = list(
                    instance.security_groups.values_list("backend_id", flat=True)
                )
                external_port_id = self._create_port_in_external_network(
                    instance.tenant, security_groups
                )
                nics.append({"port-id": external_port_id["id"]})

            # Nova microversion >= 2.36 rejects an empty nics list with a bare
            # ValueError that escapes the nova_exceptions.ClientException handler
            # below. Surface a clear backend error instead so the task is marked
            # as ERRED with an actionable message.
            if not nics:
                if ports_without_backend_id:
                    raise OpenStackBackendError(
                        "Cannot create instance %s: %d port(s) have no backend_id "
                        "(port creation likely failed earlier). Port UUIDs: %s"
                        % (
                            instance.name,
                            len(ports_without_backend_id),
                            [str(p.uuid) for p in ports_without_backend_id],
                        )
                    )
                raise OpenStackBackendError(
                    "Cannot create instance %s: at least one network port is required."
                    % instance.name
                )

            block_device_mapping_v2 = []
            for volume in instance.volumes.all():
                device_mapping = {
                    "destination_type": "volume",
                    "device_type": "disk",
                    "source_type": "volume",
                    "uuid": volume.backend_id,
                    "delete_on_termination": True,
                }
                if volume.bootable:
                    device_mapping.update({"boot_index": 0})

                block_device_mapping_v2.append(device_mapping)

            server_create_parameters = dict(
                name=instance.name,
                image=None,  # Boot from volume, see boot_index above
                flavor=backend_flavor,
                block_device_mapping_v2=block_device_mapping_v2,
                nics=nics,
                key_name=backend_public_key.name
                if backend_public_key is not None
                else None,
            )
            if instance.availability_zone:
                server_create_parameters["availability_zone"] = (
                    instance.availability_zone.name
                )
            else:
                availability_zone = instance.tenant.service_settings.options.get(
                    "availability_zone"
                )
                if availability_zone:
                    server_create_parameters["availability_zone"] = availability_zone

            if instance.user_data:
                server_create_parameters["userdata"] = instance.user_data

            if instance.config_drive is None:
                config_drive = instance.tenant.service_settings.options.get(
                    "config_drive", False
                )
            else:
                config_drive = instance.config_drive
            if config_drive:
                server_create_parameters["config_drive"] = True

            if server_group:
                server_create_parameters["scheduler_hints"] = {"group": server_group}

            # user_data may contain sensitive cloud-init payloads, so redact it
            # from the log to avoid leaking secrets into log aggregation.
            loggable_parameters = dict(server_create_parameters)
            if "userdata" in loggable_parameters:
                loggable_parameters["userdata"] = (
                    f"<redacted, {len(server_create_parameters['userdata'])} bytes>"
                )
            logger.info("Creating instance with parameters: %s", loggable_parameters)
            server = nova.servers.create(**server_create_parameters)
            instance.backend_id = server.id
            instance.save()

            logger.info(
                "Instance %s created successfully with backend_id %s. NICs used: %s",
                instance.name,
                server.id,
                nics,
            )
        except nova_exceptions.ClientException as e:
            logger.exception("Failed to provision instance %s", instance.uuid)
            message = str(e).strip() or repr(e) or type(e).__name__
            raise OpenStackBackendError(
                "Failed to provision instance '%s' in OpenStack: %s"
                % (instance.name, message)
            )
        else:
            logger.info("Successfully provisioned instance %s", instance.uuid)

    @log_backend_action()
    def pull_instance_floating_ips(self, instance: models.Instance):
        # method assumes that instance ports are up to date.
        session = get_tenant_session(instance.tenant)
        neutron = get_neutron_client(session)

        port_mappings = {
            ip.backend_id: ip for ip in instance.ports.all().exclude(backend_id="")
        }
        try:
            backend_floating_ips = neutron.list_floatingips(
                tenant_id=instance.tenant.backend_id, port_id=port_mappings.keys()
            )["floatingips"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        backend_ids = {fip["id"] for fip in backend_floating_ips}

        floating_ips = {
            fip.backend_id: fip
            for fip in models.FloatingIP.objects.filter(
                tenant=instance.tenant, backend_id__in=backend_ids
            )
        }

        with transaction.atomic():
            for backend_floating_ip in backend_floating_ips:
                imported_floating_ip = self._backend_floating_ip_to_floating_ip(
                    backend_floating_ip, instance.tenant
                )

                floating_ip = floating_ips.get(imported_floating_ip.backend_id)
                if floating_ip is None:
                    imported_floating_ip.save()
                    continue
                elif floating_ip.state == CoreStates.OK:
                    continue

                # Don't update user defined name.
                if floating_ip.address != floating_ip.name:
                    imported_floating_ip.name = floating_ip.name
                update_pulled_fields(
                    floating_ip,
                    imported_floating_ip,
                    models.FloatingIP.get_backend_fields(),
                )

                if floating_ip.port != imported_floating_ip.port:
                    floating_ip.port = imported_floating_ip.port
                    floating_ip.save()

            frontend_ids = set(
                instance.floating_ips.filter(state=CoreStates.OK)
                .exclude(backend_id="")
                .values_list("backend_id", flat=True)
            )
            stale_ids = frontend_ids - backend_ids
            if stale_ids:
                logger.info("About to detach floating IPs from ports: %s", stale_ids)
                instance.floating_ips.filter(backend_id__in=stale_ids).update(port=None)

    @log_backend_action()
    def push_instance_floating_ips(self, instance: models.Instance):
        instance_floating_ips = list(instance.floating_ips)

        # Defensive guard: update_floatingip(port_id="") silently disassociates
        # the FIP on Neutron, leaving its status stuck at DOWN. The
        # PollRuntimeStateTask scheduled after this step then retries for
        # max_retries × default_retry_delay = 1200 × 5s = 100 minutes before
        # erring out with no actionable message. Fail fast instead.
        missing = []
        for floating_ip in instance_floating_ips:
            if not floating_ip.backend_id:
                missing.append(
                    f"floating IP {floating_ip.address or floating_ip.uuid.hex} has "
                    "no backend_id (create_floating_ip did not run or failed)"
                )
            elif not floating_ip.port.backend_id:
                missing.append(
                    f"floating IP {floating_ip.address or floating_ip.uuid.hex} "
                    f"references port {floating_ip.port.uuid.hex} with empty "
                    "backend_id (port not pushed to Neutron)"
                )
        if missing:
            raise OpenStackBackendError(
                f"Cannot push floating IPs for instance {instance.name}: "
                + "; ".join(missing)
            )

        session = get_tenant_session(instance.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_floating_ips = neutron.list_floatingips(
                port_id=instance.ports.values_list("backend_id", flat=True)
            )["floatingips"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        # disconnect stale
        instance_floating_ips_ids = [fip.backend_id for fip in instance_floating_ips]
        for backend_floating_ip in backend_floating_ips:
            if backend_floating_ip["id"] not in instance_floating_ips_ids:
                try:
                    neutron.update_floatingip(
                        backend_floating_ip["id"],
                        body={"floatingip": {"port_id": None}},
                    )
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)
                else:
                    floating_ip = models.FloatingIP(
                        address=backend_floating_ip["floating_ip_address"],
                        runtime_state=backend_floating_ip["status"],
                        backend_id=backend_floating_ip["id"],
                        backend_network_id=backend_floating_ip["floating_network_id"],
                    )
                    event_logger.emit(
                        f"Floating IP {floating_ip.address} has been disconnected from instance {instance.name}.",
                        event_type=EventType.OPENSTACK_FLOATING_IP_DISCONNECTED,
                        event_context={
                            "floating_ip": floating_ip,
                            "instance": instance,
                        },
                        scopes=[floating_ip, instance],
                    )

        # connect new ones
        backend_floating_ip_ids = {fip["id"]: fip for fip in backend_floating_ips}
        for floating_ip in instance_floating_ips:
            backend_floating_ip = backend_floating_ip_ids.get(floating_ip.backend_id)
            if (
                not backend_floating_ip
                or backend_floating_ip["port_id"] != floating_ip.port.backend_id
            ):
                try:
                    neutron.update_floatingip(
                        floating_ip.backend_id,
                        body={"floatingip": {"port_id": floating_ip.port.backend_id}},
                    )
                except neutron_exceptions.NotFound as e:
                    # Neutron returns 404 here when *either* the floating IP or
                    # the port_id we passed in is not visible to the current
                    # session (deleted out-of-band, stale Waldur backend_id,
                    # cross-tenant/RBAC mismatch, OVN driver edge case, …).
                    # Silently skipping leaves the FIP unassociated while the
                    # downstream PollRuntimeStateTask spins on runtime_state
                    # `DOWN` for ~100 minutes before failing with no actionable
                    # error — fail fast with a clear diagnostic instead, naming
                    # both sides so the operator can see which one is missing.
                    raise OpenStackBackendError(
                        f"Failed to attach floating IP "
                        f"{floating_ip.address or floating_ip.uuid.hex} "
                        f"(backend_id={floating_ip.backend_id}) to port "
                        f"{floating_ip.port.uuid.hex} "
                        f"(backend_id={floating_ip.port.backend_id}) on "
                        f"instance {instance.name}: Neutron returned NotFound. "
                        f"The floating IP or the target port is not visible in "
                        f"the tenant session (deleted out-of-band, stale "
                        f"Waldur backend_id, or cross-tenant/RBAC mismatch). "
                        f"Original error: {e}"
                    ) from e
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)
                else:
                    event_logger.emit(
                        f"Floating IP {floating_ip.address} has been connected to instance {instance.name}.",
                        event_type=EventType.OPENSTACK_FLOATING_IP_CONNECTED,
                        event_context={
                            "floating_ip": floating_ip,
                            "instance": instance,
                        },
                        scopes=[floating_ip, instance],
                    )

    def _get_or_create_ssh_key(
        self, tenant: models.Tenant, key_name, fingerprint_md5, public_key
    ):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)

        try:
            return nova.keypairs.find(fingerprint=fingerprint_md5)
        except nova_exceptions.NotFound:
            # Fine, it's a new key, let's add it
            try:
                # Remove all whitespaces, just in case
                key_name = key_name.translate(str.maketrans("", "", " \n\t\r"))
                logger.info("Propagating ssh public key %s to backend", key_name)
                return nova.keypairs.create(name=key_name, public_key=public_key)
            except nova_exceptions.ClientException as e:
                logger.error(
                    "Unable to import SSH public key to OpenStack, "
                    "key_name: %s, fingerprint_md5: %s, public_key: %s, error: %s",
                    key_name,
                    fingerprint_md5,
                    public_key,
                    e,
                )
                raise OpenStackBackendError(e)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def update_instance(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.update(
                instance.backend_id,
                name=instance.name,
                description=instance.description,
            )
        except keystone_exceptions.NotFound as e:
            raise OpenStackBackendError(e)

    def import_instance(
        self,
        tenant: models.Tenant,
        backend_id,
        project=None,
        save=True,
        connected_internal_network_names=None,
    ):
        # NB! This method does not import instance sub-objects like security groups or ports.
        #     They have to be pulled separately.

        if connected_internal_network_names is None:
            connected_internal_network_names = set()

        # hypervisor_hostname forces to use admin session
        session = get_keystone_session(tenant.service_settings)
        nova = get_nova_client(session)

        try:
            backend_instance = nova.servers.get(backend_id)
            attached_volume_ids = [
                v.volumeId for v in nova.volumes.get_server_volumes(backend_id)
            ]
            backend_flavor = backend_instance.flavor
            image_id = backend_instance.image and backend_instance.image.get("id")

            # If no image_id from instance metadata, try to get it from bootable volumes
            detected_image_name = None
            if not image_id:
                detected_image_id, detected_image_name = (
                    self._detect_image_from_bootable_volumes(
                        tenant, attached_volume_ids, backend_instance
                    )
                )
                # First try to use detected_image_id if it corresponds to an existing Image in Waldur
                if detected_image_id:
                    try:
                        models.Image.objects.get(
                            settings=tenant.service_settings,
                            backend_id=detected_image_id,
                        )
                        image_id = detected_image_id
                        detected_image_name = (
                            None  # Clear image name since we're using image_id
                        )
                    except models.Image.DoesNotExist:
                        # If image_id doesn't exist in Waldur, use image_name directly
                        # Don't try to convert image_name back to image_id
                        pass
        except nova_exceptions.NotFound:
            raise
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        instance: models.Instance = self._backend_instance_to_instance(
            tenant,
            backend_instance,
            backend_flavor,
            connected_internal_network_names,
            image_id,
            detected_image_name,
        )
        with transaction.atomic():
            instance.tenant = tenant
            instance.service_settings = tenant.service_settings
            instance.project = project
            if hasattr(backend_instance, "fault"):
                instance.error_message = backend_instance.fault["message"]
            if save:
                instance.save()
                volumes = self._import_instance_volumes(
                    tenant, attached_volume_ids, project, save
                )
                instance.volumes.add(*volumes)

        return instance

    def _import_instance_volumes(
        self, tenant: models.Tenant, attached_volume_ids, project, save
    ):
        # import instance volumes, or use existed if they already exist in Waldur.
        volumes = []
        for backend_volume_id in attached_volume_ids:
            try:
                volumes.append(
                    models.Volume.objects.get(
                        tenant=tenant, backend_id=backend_volume_id
                    )
                )
            except models.Volume.DoesNotExist:
                try:
                    volumes.append(
                        self.import_volume(tenant, backend_volume_id, project, save)
                    )
                except OpenStackAuthorizationFailed as e:
                    # Authentication failed, can't continue with import
                    logger.error(
                        "Authentication failed while importing volume %s for tenant %s: %s. Cannot continue import.",
                        backend_volume_id,
                        tenant.uuid,
                        e,
                    )
                    raise  # Re-raise auth failures as they affect the entire import process
                except OpenStackBackendError as e:
                    # Volume no longer exists in OpenStack, log and skip
                    logger.warning(
                        "Volume %s attached to instance could not be imported: %s. Skipping.",
                        backend_volume_id,
                        e,
                    )
        return volumes

    def _detect_image_from_bootable_volumes(
        self, tenant: models.Tenant, attached_volume_ids, backend_instance=None
    ):
        """
        Detect image ID or image name from bootable volumes when instance metadata doesn't contain image info.
        This is useful for instances booted from volumes where the original image reference is lost.

        Uses existing Volume records in Waldur database that already have image_metadata populated.

        Prioritizes volumes in this order:
        1. Boot volume (attached to root device like /dev/vda)
        2. First bootable volume in attachment order
        3. Any bootable volume with image metadata

        Returns:
            tuple: (image_id, image_name) where image_id can be None if not found,
                   but image_name might still be available for fallback lookup
        """
        bootable_volumes = []
        root_device_name = None

        # Get root device name if backend_instance is provided
        if backend_instance:
            # OpenStack uses underscored attribute names in the client
            root_device_name = getattr(
                backend_instance, "OS-EXT-SRV-ATTR:root_device_name", None
            )
            # If that doesn't work, try accessing via dict-like interface
            if not root_device_name and hasattr(backend_instance, "to_dict"):
                instance_dict = backend_instance.to_dict()
                root_device_name = instance_dict.get("OS-EXT-SRV-ATTR:root_device_name")

        # Look up volumes in Waldur database by their backend_id
        # Process volumes in the order they appear in attached_volume_ids (attachment order)
        for order_index, backend_volume_id in enumerate(attached_volume_ids):
            try:
                # Find the volume in Waldur database
                volume = models.Volume.objects.get(
                    tenant=tenant, backend_id=backend_volume_id
                )

                # Check if volume is bootable and has image info
                if volume.bootable:
                    image_id = None
                    image_name = None

                    if volume.image_metadata:
                        image_id = volume.image_metadata.get("image_id")
                        image_name = volume.image_metadata.get("image_name")

                    # Fall back to volume.image FK if image_metadata is empty
                    if not image_id and volume.image_id:
                        image_id = volume.image.backend_id
                        image_name = volume.image.name

                    # Include volume if it has either image_id or image_name
                    if image_id or image_name:
                        # Check if this volume is attached to the root device
                        is_root_volume = (
                            (volume.device == root_device_name)
                            if root_device_name
                            else False
                        )

                        bootable_volumes.append(
                            {
                                "volume_id": backend_volume_id,
                                "image_id": image_id,
                                "image_name": image_name,
                                "is_root": is_root_volume,
                                "device": volume.device,
                                "order": order_index,
                                "has_image_id": bool(image_id),
                            }
                        )
            except models.Volume.DoesNotExist:
                # Volume not yet imported in Waldur, skip it
                continue

        if not bootable_volumes:
            return None, None

        # Sort bootable volumes by priority:
        # 1. Root volumes first (identified by root_device_name)
        # 2. Volumes with image_id over volumes with only image_name
        # 3. Then by attachment order (first attached volume)
        # 4. Then by device name (vda comes before vdb, etc.)
        def volume_priority(vol):
            if vol["is_root"]:
                return (0, not vol["has_image_id"], vol["order"], vol["device"])
            return (1, not vol["has_image_id"], vol["order"], vol["device"])

        bootable_volumes.sort(key=volume_priority)

        # Return both image_id and image_name from the highest priority bootable volume
        best_volume = bootable_volumes[0]
        return best_volume["image_id"], best_volume["image_name"]

    def _backend_instance_to_instance(
        self,
        tenant: models.Tenant,
        backend_instance,
        backend_flavor=None,
        connected_internal_network_names=None,
        backend_image_id=None,
        backend_image_name=None,
    ):
        launch_time = None
        # parse launch time
        try:
            d = dateparse.parse_datetime(
                backend_instance.to_dict()["OS-SRV-USG:launched_at"]
            )
        except (KeyError, ValueError, TypeError):
            launch_time = None
        else:
            # At the moment OpenStack does not provide any timezone info,
            # but in future it might do.
            if timezone.is_naive(d):
                launch_time = timezone.make_aware(d, UTC)

        availability_zone = None
        try:
            availability_zone_name = (
                backend_instance.to_dict().get("OS-EXT-AZ:availability_zone") or ""
            )
            hypervisor_hostname = (
                backend_instance.to_dict().get("OS-EXT-SRV-ATTR:hypervisor_hostname")
                or ""
            )

            if availability_zone_name:
                availability_zone = models.InstanceAvailabilityZone.objects.get(
                    name=availability_zone_name, tenant=tenant
                )
        except (
            KeyError,
            ValueError,
            TypeError,
            models.InstanceAvailabilityZone.DoesNotExist,
        ):
            pass
        if connected_internal_network_names is None:
            connected_internal_network_names = set()
        backend_networks = getattr(backend_instance, "networks", None) or {}
        external_backend_networks = (
            set(backend_networks.keys()) - connected_internal_network_names
        )
        external_backend_ips = [
            ",".join(backend_networks[ext_net]) for ext_net in external_backend_networks
        ]

        # Microversion 2.69 partial-cells responses can return `created=None`,
        # so guard against TypeError from dateparse.parse_datetime(None).
        created_raw = getattr(backend_instance, "created", None)
        try:
            created = dateparse.parse_datetime(created_raw) if created_raw else None
        except (ValueError, TypeError):
            created = None

        instance = models.Instance(
            name=backend_instance.name or backend_instance.id,
            key_name=backend_instance.key_name or "",
            start_time=launch_time,
            state=CoreStates.OK,
            runtime_state=backend_instance.status,
            created=created,
            backend_id=backend_instance.id,
            availability_zone=availability_zone,
            hypervisor_hostname=hypervisor_hostname,
            directly_connected_ips=",".join(external_backend_ips),
        )

        # With Nova microversion 2.47+, flavor details are embedded in the
        # server response (vcpus, ram, disk, original_name) even for deleted flavors.
        if backend_flavor:
            instance.flavor_name = backend_flavor.get("original_name", "")
            instance.flavor_disk = self.gb2mb(backend_flavor.get("disk", 0))
            instance.cores = backend_flavor.get("vcpus", 0)
            instance.ram = backend_flavor.get("ram", 0)

        if backend_image_id:
            try:
                image = models.Image.objects.get(
                    settings=tenant.service_settings, backend_id=backend_image_id
                )
                instance.image_name = image.name
            except models.Image.DoesNotExist:
                backend_image = self._get_image(tenant, backend_image_id)
                # If image has been removed in OpenStack cloud, we should skip update
                if backend_image:
                    instance.image_name = str(
                        backend_image.name
                    )  # Ensure string conversion
                else:
                    # Image not found in backend — leave image_name empty on this
                    # transient object. pull_instance() will skip overwriting
                    # the DB instance's image_name if it already has a value.
                    pass
        elif backend_image_name:
            # Use the provided image name directly (from volume metadata fallback)
            instance.image_name = str(backend_image_name)  # Ensure string conversion

        attached_volumes = backend_instance.to_dict().get(
            "os-extended-volumes:volumes_attached", []
        )
        attached_volume_ids = [volume["id"] for volume in attached_volumes]
        volumes = self._import_instance_volumes(
            tenant, attached_volume_ids, project=None, save=False
        )
        instance.disk = sum(volume.size for volume in volumes)

        return instance

    def _get_image(self, tenant: models.Tenant, image_id):
        session = get_tenant_session(tenant)
        glance = get_glance_client(session)

        try:
            return glance.images.get(image_id)
        except glance_exceptions.NotFound:
            logger.info("OpenStack image %s is gone.", image_id)
            return None
        except glance_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def get_instances(self, tenant: models.Tenant) -> list[models.Instance]:
        nova = get_nova_client(self.admin_session)

        try:
            # We use search_opts according to the rules in
            # https://docs.openstack.org/api-ref/compute/?expanded=list-servers-detail#list-server-request
            backend_instances = nova.servers.list(
                search_opts={"project_id": tenant.backend_id, "all_tenants": 1}
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        instances = []
        for backend_instance in backend_instances:
            backend_flavor = backend_instance.flavor
            image_id = backend_instance.image and backend_instance.image.get("id")
            instances.append(
                self._backend_instance_to_instance(
                    tenant, backend_instance, backend_flavor, None, image_id, None
                )
            )
        return instances

    def get_importable_instances(self, tenant: models.Tenant) -> list[dict]:
        instances = [
            {
                "type": get_resource_type(models.Instance),
                "name": instance.name,
                "backend_id": instance.backend_id,
                "description": instance.description,
                "extra": [
                    {"name": "Runtime state", "value": instance.runtime_state},
                    {"name": "Flavor", "value": instance.flavor_name},
                    {"name": "RAM (MBs)", "value": instance.ram},
                    {"name": "Cores", "value": instance.cores},
                ],
            }
            for instance in self.get_instances(tenant)
        ]
        return self.get_importable_resources(models.Instance, instances)

    def get_expired_resources(
        self, tenant: models.Tenant, resource_model, remote_resources_ids
    ):
        local_resources = resource_model.objects.filter(tenant=tenant)
        result = []
        for resource in local_resources:
            if resource.backend_id not in remote_resources_ids:
                result.append(resource)
        return result

    def get_expired_instances(self, tenant: models.Tenant) -> list[models.Instance]:
        instances = [instance.backend_id for instance in self.get_instances(tenant)]
        return self.get_expired_resources(tenant, models.Instance, instances)

    def get_expired_volumes(self, tenant: models.Tenant) -> list[models.Volume]:
        volumes = [volumes.backend_id for volumes in self.get_volumes(tenant)]
        return self.get_expired_resources(tenant, models.Volume, volumes)

    def get_importable_volumes(self, tenant: models.Tenant):
        volumes = [
            {
                "type": get_resource_type(models.Volume),
                "name": volume.name,
                "backend_id": volume.backend_id,
                "description": volume.description,
                "extra": [
                    {"name": "Is bootable", "value": volume.bootable},
                    {"name": "Size", "value": volume.size},
                    {"name": "Device", "value": volume.device},
                    {"name": "Runtime state", "value": volume.runtime_state},
                ],
            }
            for volume in self.get_volumes(tenant)
        ]
        return self.get_importable_resources(models.Volume, volumes)

    @transaction.atomic()
    def _pull_zones(
        self, tenant: models.Tenant, backend_zones, frontend_model, default_zone="nova"
    ):
        """
        This method is called for Volume and Instance Availability zone synchronization.
        It is assumed that default zone could not be used for Volume or Instance provisioning.
        Therefore we do not pull default zone at all. Please note, however, that default zone
        name could be changed in Nova and Cinder config. We don't support this use case either.


        All availability zones are split into 3 subsets: stale, missing and common.
        Stale zone are removed, missing zones are created.
        If zone state has been changed, it is synchronized.
        """
        # Availability zones are global per service settings, not per tenant.
        # Sync using settings-only scope to avoid duplicate records per tenant.
        front_zones_map = {
            zone.name: zone
            for zone in frontend_model.objects.filter(settings=self.settings)
        }

        back_zones_map = {
            zone.zoneName: zone.zoneState.get("available", True)
            for zone in backend_zones
            if zone.zoneName != default_zone
        }

        missing_zones = set(back_zones_map.keys()) - set(front_zones_map.keys())
        for zone in missing_zones:
            frontend_model.objects.get_or_create(
                settings=self.settings,
                name=zone,
                defaults={"available": back_zones_map[zone]},
            )

        stale_zones = set(front_zones_map.keys()) - set(back_zones_map.keys())
        frontend_model.objects.filter(
            name__in=stale_zones, settings=self.settings
        ).delete()

        common_zones = set(front_zones_map.keys()) & set(back_zones_map.keys())
        for zone_name in common_zones:
            zone = front_zones_map[zone_name]
            actual = back_zones_map[zone_name]
            if zone.available != actual:
                zone.available = actual
                zone.save(update_fields=["available"])

    def pull_tenant_instance_availability_zones(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)
        try:
            # By default detailed flag is True, but OpenStack policy for detailed data is disabled.
            # Therefore we should explicitly pass detailed=False. Otherwise request fails.
            backend_zones = nova.availability_zones.list(detailed=False)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        self._pull_zones(tenant, backend_zones, models.InstanceAvailabilityZone)

    @log_backend_action()
    def pull_instance(self, instance: models.Instance, update_fields=None):
        import_time = timezone.now()
        connected_internal_network_names = set(
            instance.ports.all().values_list("subnet__network__name", flat=True)
        )
        imported_instance = self.import_instance(
            instance.tenant,
            instance.backend_id,
            save=False,
            connected_internal_network_names=connected_internal_network_names,
        )

        instance.refresh_from_db()
        if instance.modified < import_time:
            if update_fields is None:
                update_fields = models.Instance.get_backend_fields()
            # Don't overwrite image_name with empty value if instance already has one
            if (
                not imported_instance.image_name
                and instance.image_name
                and "image_name" in update_fields
            ):
                update_fields = tuple(f for f in update_fields if f != "image_name")
            # Don't overwrite flavor fields with zeros if instance already has values
            for field in ("cores", "ram", "flavor_name", "flavor_disk"):
                if (
                    not getattr(imported_instance, field)
                    and getattr(instance, field)
                    and field in update_fields
                ):
                    update_fields = tuple(f for f in update_fields if f != field)
            update_pulled_fields(instance, imported_instance, update_fields)

    @log_backend_action()
    def pull_instance_ports(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_ports = neutron.list_ports(device_id=instance.backend_id)["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        existing_ips = {
            ip.backend_id: ip for ip in instance.ports.exclude(backend_id="")
        }

        pending_ips = {
            ip.subnet.backend_id: ip for ip in instance.ports.filter(backend_id="")
        }

        local_ips = {
            ip.backend_id: ip
            for ip in (
                models.Port.objects.filter(tenant=instance.tenant).exclude(
                    backend_id=""
                )
            )
        }

        # Enhanced logging for debugging shared network port issues
        logger.debug(
            "Port matching sets for instance %s (tenant: %s):",
            instance.name,
            instance.tenant.uuid,
        )
        logger.debug(
            "- existing_ips (%d ports): %s",
            len(existing_ips),
            {
                k: f"port_{str(v.uuid)[:8]}(tenant_{str(v.tenant.uuid)[:8]})"
                for k, v in existing_ips.items()
            },
        )
        logger.debug(
            "- pending_ips (%d ports): %s",
            len(pending_ips),
            {
                k: f"port_{str(v.uuid)[:8]}(tenant_{str(v.tenant.uuid)[:8]})"
                for k, v in pending_ips.items()
            },
        )
        logger.debug(
            "- local_ips (%d ports): %s",
            len(local_ips),
            {
                k: f"port_{str(v.uuid)[:8]}(tenant_{str(v.tenant.uuid)[:8]})"
                for k, v in local_ips.items()
            },
        )

        subnets = instance.tenant.available_subnets

        subnet_mappings = {subnet.backend_id: subnet for subnet in subnets}

        logger.info(
            "Pulling %d ports from OpenStack for instance %s (backend_id: %s)",
            len(backend_ports),
            instance.name,
            instance.backend_id,
        )

        with transaction.atomic():
            for backend_port in backend_ports:
                logger.info(
                    "Processing backend port %s: fixed_ips=%s, mac_address=%s, status=%s",
                    backend_port["id"],
                    backend_port.get("fixed_ips", []),
                    backend_port.get("mac_address"),
                    backend_port.get("status"),
                )

                imported_port = self.parse_backend_port(backend_port, instance=instance)
                subnet = subnet_mappings.get(imported_port._subnet_backend_id)

                # Enhanced logging for port matching decisions
                logger.debug(
                    "Port matching analysis for backend_port %s:",
                    imported_port.backend_id,
                )
                logger.debug(
                    "  - subnet_backend_id: %s (mapped to subnet: %s)",
                    imported_port._subnet_backend_id,
                    str(subnet.uuid)[:8] if subnet else "None",
                )
                logger.debug(
                    "  - in pending_ips: %s",
                    imported_port._subnet_backend_id in pending_ips,
                )
                logger.debug(
                    "  - in existing_ips: %s",
                    imported_port.backend_id in existing_ips,
                )
                logger.debug(
                    "  - in local_ips: %s",
                    imported_port.backend_id in local_ips,
                )
                if imported_port.backend_id in local_ips:
                    existing_port = local_ips[imported_port.backend_id]
                    logger.debug(
                        "  - local_ips port details: port_%s (instance: %s, tenant: %s)",
                        str(existing_port.uuid)[:8],
                        str(existing_port.instance.uuid)[:8]
                        if existing_port.instance
                        else "None",
                        str(existing_port.tenant.uuid)[:8],
                    )
                    logger.debug(
                        "  - current instance: %s (tenant: %s)",
                        str(instance.uuid)[:8],
                        str(instance.tenant.uuid)[:8],
                    )

                if subnet is None:
                    logger.warning(
                        "Skipping Neutron port synchronization process because "
                        "related subnet is not imported yet. Port ID: %s, subnet ID: %s",
                        imported_port.backend_id,
                        imported_port._subnet_backend_id,
                    )
                    continue

                if imported_port._subnet_backend_id in pending_ips:
                    port = pending_ips[imported_port._subnet_backend_id]
                    logger.debug(
                        "[PATH: PENDING] Updating pending port %s: old_fixed_ips=%s, new_fixed_ips=%s, new_backend_id=%s",
                        port.uuid,
                        port.fixed_ips,
                        imported_port.fixed_ips,
                        imported_port.backend_id,
                    )
                    # Update backend ID for pending port
                    update_pulled_fields(
                        port,
                        imported_port,
                        models.Port.get_backend_fields() + ("backend_id",),
                    )

                elif imported_port.backend_id in existing_ips:
                    port = existing_ips[imported_port.backend_id]
                    logger.debug(
                        "[PATH: EXISTING] Updating existing port %s: old_fixed_ips=%s, new_fixed_ips=%s",
                        port.uuid,
                        port.fixed_ips,
                        imported_port.fixed_ips,
                    )
                    update_pulled_fields(
                        port,
                        imported_port,
                        models.Port.get_backend_fields(),
                    )
                    if subnet and not port.subnet:
                        port.subnet = subnet
                        port.save()

                elif imported_port.backend_id in local_ips:
                    port = local_ips[imported_port.backend_id]
                    if port.instance != instance:
                        logger.warning(
                            "[PATH: LOCAL-REASSIGN] *** POTENTIAL ISSUE *** About to reassign shared port %s from instance %s (tenant %s) to instance %s (tenant %s). "
                            "This might be the cause of the wrong port assignment!",
                            str(port.uuid)[:8],
                            str(port.instance.uuid)[:8] if port.instance else "None",
                            str(port.tenant.uuid)[:8],
                            str(instance.uuid)[:8],
                            str(instance.tenant.uuid)[:8],
                        )
                        port.instance = instance
                        port.save()
                    else:
                        logger.debug(
                            "[PATH: LOCAL-UPDATE] Port %s already assigned to correct instance",
                            str(port.uuid)[:8],
                        )

                else:
                    logger.debug(
                        "[PATH: NEW] Creating new port from OpenStack data. Instance ID: %s, subnet ID: %s, "
                        "backend_port_id: %s, fixed_ips: %s",
                        instance.backend_id,
                        subnet.backend_id,
                        imported_port.backend_id,
                        imported_port.fixed_ips,
                    )
                    port = imported_port
                    port.subnet = subnet
                    port.project = subnet.project
                    port.tenant = subnet.tenant
                    port.network = subnet.network
                    port.service_settings = subnet.service_settings
                    port.instance = instance
                    port.save()
                    logger.info(
                        "New port created and saved: %s with fixed_ips: %s",
                        port.uuid,
                        port.fixed_ips,
                    )

            # remove stale ports
            frontend_ids = set(existing_ips.keys())
            backend_ids = {port["id"] for port in backend_ports}
            stale_ids = frontend_ids - backend_ids
            if stale_ids:
                logger.info("About to delete ports with IDs %s", stale_ids)
                instance.ports.filter(backend_id__in=stale_ids).delete()

            # finally, mark all instance ports with backend_id as OK
            instance.ports.exclude(backend_id="").update(state=CoreStates.OK)

    def _find_reusable_port(self, admin_neutron, local_port: models.Port):
        """Find a leftover Neutron port holding the address `local_port` asks for.

        Returns the port to adopt, or None when the address is held by something we
        must not touch — in which case the caller re-raises the allocation error.

        Ports stranded by an earlier failed attach keep their address allocated
        while no longer being reachable: they are unbound, so they never show up in
        `list_ports(device_id=...)`, and the local row that would have named them
        was never given a backend_id. Adopting one is only safe when it sits in the
        expected subnet, belongs to the same tenant, is attached to nothing, and is
        claimed by no other local port.
        """
        wanted = {
            (fixed_ip["subnet_id"], fixed_ip["ip_address"])
            for fixed_ip in local_port.fixed_ips or []
            if fixed_ip.get("subnet_id") and fixed_ip.get("ip_address")
        }
        if not wanted:
            return None

        subnet_id, ip_address = sorted(wanted)[0]
        candidates = admin_neutron.list_ports(
            fixed_ips=[f"subnet_id={subnet_id}", f"ip_address={ip_address}"]
        )["ports"]

        for candidate in candidates:
            if candidate.get("device_id") or candidate.get("device_owner"):
                continue
            if candidate.get("tenant_id") != local_port.tenant.backend_id:
                continue
            found = {
                (fixed_ip["subnet_id"], fixed_ip["ip_address"])
                for fixed_ip in candidate["fixed_ips"]
            }
            if found != wanted:
                continue
            if models.Port.objects.filter(backend_id=candidate["id"]).exists():
                continue
            return candidate
        return None

    @log_backend_action()
    def push_instance_ports(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        # we assume that port subnet cannot be changed
        neutron = get_neutron_client(session)
        nova = get_nova_client(session)
        # Port creation is done through the admin session, consistent with
        # create_port() and create_instance_port(). A stored port carries the
        # concrete fixed IP pulled from the backend, and specifying an explicit
        # ip_address on create is admin-only in Neutron
        # (create_port:fixed_ips:ip_address), so the tenant session is refused.
        admin_neutron = get_neutron_client(self.admin_session)

        try:
            backend_ports = neutron.list_ports(device_id=instance.backend_id)["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        # delete stale ports
        existing_instance_ids = set(instance.ports.values_list("backend_id", flat=True))
        for backend_port in backend_ports:
            if backend_port["id"] not in existing_instance_ids:
                try:
                    logger.info(
                        "About to delete network port with ID %s.",
                        backend_port["id"],
                    )
                    neutron.delete_port(backend_port["id"])
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)

        # create or attach ports not yet on the instance
        new_ports = instance.ports.exclude(
            backend_id__in=[ip["id"] for ip in backend_ports]
        )
        for new_port in new_ports:
            try:
                if new_port.backend_id:
                    # Existing port — attach it to the instance directly
                    logger.debug(
                        "About to attach existing port %s to instance %s.",
                        new_port.backend_id,
                        instance.backend_id,
                    )
                    nova.servers.interface_attach(
                        instance.backend_id, new_port.backend_id, None, None
                    )
                else:
                    # New port — create in OpenStack and attach.
                    # The port is created via the admin session, so ownership
                    # must be set explicitly: without tenant_id Neutron places
                    # the port in the admin project and the tenant-scoped
                    # interface_attach below fails with 404 (port not visible
                    # across projects).
                    port_payload = {
                        "network_id": new_port.subnet.network.backend_id,
                        "tenant_id": new_port.tenant.backend_id,
                        "project_id": new_port.tenant.backend_id,
                        "fixed_ips": new_port.fixed_ips
                        if new_port.fixed_ips
                        else [
                            {
                                "subnet_id": new_port.subnet.backend_id,
                            }
                        ],
                        "security_groups": list(
                            instance.security_groups.exclude(backend_id="").values_list(
                                "backend_id", flat=True
                            )
                        ),
                    }
                    logger.debug(
                        "About to create network port for instance %s in subnet %s.",
                        instance.backend_id,
                        new_port.subnet.backend_id,
                    )
                    try:
                        created_port = admin_neutron.create_port(
                            {"port": port_payload}
                        )["port"]
                    except (
                        neutron_exceptions.IpAddressAlreadyAllocatedClient,
                        neutron_exceptions.IpAddressInUseClient,
                    ):
                        created_port = self._find_reusable_port(admin_neutron, new_port)
                        if created_port is None:
                            raise
                        logger.info(
                            "Reusing unattached port %s, which already holds the "
                            "address requested for instance %s.",
                            created_port["id"],
                            instance.backend_id,
                        )
                    # Record the port before attaching it. An attach failure used to
                    # discard the id, leaving the port in Neutron with its address
                    # still allocated and nothing referencing it, so every later run
                    # tried to create it again and failed on that same address.
                    new_port.mac_address = created_port["mac_address"]
                    new_port.fixed_ips = created_port["fixed_ips"]
                    new_port.backend_id = created_port["id"]
                    new_port.save()
                    nova.servers.interface_attach(
                        instance.backend_id, created_port["id"], None, None
                    )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)
            except nova_exceptions.ClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def create_instance_ports(self, instance: models.Instance):
        security_groups = list(
            instance.security_groups.values_list("backend_id", flat=True)
        )
        for port in instance.ports.all():
            if not port.backend_id:
                self.create_instance_port(port, security_groups)

    def create_instance_port(self, port: models.Port, instance_security_groups):
        # Use admin session for all port creation to ensure consistent behavior
        # and avoid authorization issues with shared networks
        session = self.admin_session
        neutron = get_neutron_client(session)
        security_groups = []

        logger.info(
            "About to create network port. Network ID: %s. Subnet ID: %s. Port name: %s. "
            "Network tenant: %s, Instance tenant: %s",
            port.subnet.network.backend_id,
            port.subnet.backend_id,
            port.name,
            port.network.tenant.backend_id,
            port.tenant.backend_id,
        )

        # Log initial port state
        logger.info(
            "Initial port state - fixed_ips: %s, mac_address: %s, port_tenant_id: %s",
            port.fixed_ips,
            port.mac_address,
            port.tenant.backend_id,
        )

        # Enhanced logging for security group assignment
        logger.debug("Security group assignment analysis:")
        logger.debug(
            "  - Instance security groups: %s",
            [f"{sg_id}" for sg_id in instance_security_groups],
        )
        logger.debug(
            "  - Network tenant != Instance tenant: %s (network: %s, instance: %s)",
            port.instance and (port.network.tenant != port.instance.tenant),
            port.network.tenant.uuid,
            port.instance.tenant.uuid if port.instance else "None",
        )

        if port.instance and (port.network.tenant != port.instance.tenant):
            # RBAC shared network scenario: security groups from instance tenant should be used directly
            # The original logic tried to find matching security groups in the network tenant,
            # but this is incorrect - security groups belong to the instance tenant
            logger.debug(
                "[SHARED NETWORK] Using instance security groups directly for RBAC shared network: %s",
                instance_security_groups,
            )
            security_groups = instance_security_groups
        else:
            logger.debug(
                "[NON-SHARED NETWORK] Using instance security groups directly: %s",
                instance_security_groups,
            )
            security_groups = instance_security_groups

        # For shared networks, we need to ensure the port is created in the correct tenant context
        # The tenant_id should be the instance's tenant (where the port logically belongs)
        # but the session should be from the network owner for authorization
        port_payload = {
            "name": port.name,
            "description": port.description,
            "network_id": port.subnet.network.backend_id,
            "tenant_id": port.tenant.backend_id,  # Instance tenant (where port belongs)
            "project_id": port.tenant.backend_id,  # Instance tenant (where port belongs)
            "fixed_ips": [
                {
                    "subnet_id": port.subnet.backend_id,
                }
            ],
            "port_security_enabled": port.port_security_enabled,
        }

        if port.port_security_enabled:
            port_payload["security_groups"] = security_groups

        logger.debug(
            "Final port payload security_groups: %s (count: %d)",
            security_groups,
            len(security_groups),
        )

        logger.info(
            "Port payload tenant context - tenant_id: %s, network_owner: %s, session_type: admin",
            port.tenant.backend_id,
            port.network.tenant.backend_id,
        )

        if port.mac_address:
            port_payload["mac_address"] = port.mac_address

        if port.fixed_ips:
            port_payload["fixed_ips"] = port.fixed_ips
            logger.info(
                "Using pre-defined fixed_ips from port model: %s",
                port.fixed_ips,
            )
        else:
            logger.info(
                "No pre-defined fixed_ips, letting OpenStack auto-assign from subnet %s",
                port.subnet.backend_id,
            )

        logger.info(
            "Port creation payload: %s",
            port_payload,
        )

        try:
            backend_port = neutron.create_port({"port": port_payload})["port"]
        except neutron_exceptions.IpAddressAlreadyAllocatedClient as e:
            # Neutron refused the requested fixed IP — already in use on the
            # subnet. This is a recoverable user/race condition rather than an
            # internal failure, so:
            #   - log at WARNING (no traceback) in backend.py;
            #   - raise OpenStackBackendError with a human-readable message so
            #     it surfaces clearly in instance.error_message instead of the
            #     default cryptic "An unknown exception occurred." that the
            #     bare exception repr produces.
            requested_ips = ", ".join(
                f.get("ip_address")
                for f in (port_payload.get("fixed_ips") or [])
                if f.get("ip_address")
            )
            if requested_ips:
                detail = f"requested fixed IP {requested_ips} is already allocated"
            else:
                detail = "the IP allocated for this port is already in use"
            user_message = (
                f"Failed to create port on subnet {port.subnet.backend_id}: "
                f"{detail}. Choose a different IP address or omit fixed_ips "
                "to let OpenStack auto-assign one."
            )
            logger.warning(
                "%s Network: %s",
                user_message,
                port_payload.get("network_id"),
            )
            raise OpenStackBackendError(user_message) from e
        except neutron_exceptions.NeutronClientException as e:
            logger.error(
                "Failed to create port. Payload was: %s. Error: %s",
                port_payload,
                e,
            )
            raise OpenStackBackendError(e)

        logger.info(
            "OpenStack returned port data - id: %s, mac_address: %s, fixed_ips: %s, status: %s",
            backend_port["id"],
            backend_port["mac_address"],
            backend_port["fixed_ips"],
            backend_port.get("status", "unknown"),
        )

        # Log before saving to Waldur
        logger.info(
            "Before saving to Waldur - port.fixed_ips was: %s, will be set to: %s",
            port.fixed_ips,
            backend_port["fixed_ips"],
        )

        port.mac_address = backend_port["mac_address"]
        port.fixed_ips = backend_port["fixed_ips"]
        port.backend_id = backend_port["id"]
        port.admin_state_up = backend_port["admin_state_up"]
        port.port_security_enabled = backend_port["port_security_enabled"]
        port.device_owner = backend_port["device_owner"]
        port.status = backend_port["status"]
        port.state = CoreStates.OK  # Set port state to OK after successful creation
        port.save()

        logger.info(
            "Port successfully created and saved. Waldur port ID: %s, Backend ID: %s, "
            "Fixed IPs: %s, State: %s, Admin State: %s, Status: %s, Device Owner: %s",
            port.uuid,
            port.backend_id,
            port.fixed_ips,
            port.state,
            port.admin_state_up,
            port.status,
            port.device_owner,
        )

    @log_backend_action()
    def update_port_name_and_description(self, port: models.Port):
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)

        port_payload = {
            "name": port.name,
            "description": port.description,
        }

        try:
            neutron.update_port(port.backend_id, {"port": port_payload})
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            event_logger.emit(
                f"Port [{port}] name and description have been updated in the backend.",
                event_type=EventType.OPENSTACK_PORT_UPDATED,
                event_context={"port": port},
                scopes=[port, port.network],
            )

    @log_backend_action()
    def delete_instance_ports(self, instance: models.Instance):
        for port in instance.ports.all():
            if port.backend_id:
                self.delete_instance_port(port)

    def delete_instance_port(self, port: models.Port):
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)

        logger.debug("About to delete network port. Port ID: %s.", port.backend_id)
        try:
            neutron.delete_port(port.backend_id)
        except neutron_exceptions.NotFound:
            logger.debug(
                "Neutron port is already deleted. Backend ID: %s.",
                port.backend_id,
            )
        except neutron_exceptions.NeutronClientException as e:
            logger.warning(
                "Unable to delete OpenStack network port. "
                "Skipping error and trying to continue instance deletion. "
                "Backend ID: %s. Error message is: %s",
                port.backend_id,
                e,
            )
        port.delete()

    @log_backend_action()
    def push_instance_allowed_address_pairs(
        self, instance: models.Instance, backend_id, allowed_address_pairs
    ):
        session = get_tenant_session(instance.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.update_port(
                backend_id, {"port": {"allowed_address_pairs": allowed_address_pairs}}
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def set_port_allowed_address_pairs(
        self, port: models.Port, allowed_address_pairs: list[dict]
    ):
        """Replace a Port's allowed_address_pairs in Neutron and re-pull.

        Used by ``PortViewSet.set_allowed_address_pairs`` so cluster-VIP
        workloads (keepalived, MetalLB, OpenShift ingress, OVN router-as-VM)
        can permit additional IP/MAC pairs on their ports.
        """
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.update_port(
                port.backend_id,
                {"port": {"allowed_address_pairs": allowed_address_pairs}},
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def pull_instance_security_groups(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        server_id = instance.backend_id

        logger.info(
            f"Starting security group sync for instance {instance.name} "
            f"(UUID: {instance.uuid}, backend_id: {server_id}) "
            f"in tenant {instance.tenant.name}"
        )

        try:
            remote_groups = nova.servers.list_security_group(server_id)
            logger.info(
                f"Nova API returned {len(remote_groups)} security groups for server {server_id}: "
                f"{[{'id': g.id, 'name': getattr(g, 'name', 'unknown')} for g in remote_groups]}"
            )
        except nova_exceptions.ClientException as e:
            logger.error(
                f"Failed to fetch security groups from Nova API for server {server_id} "
                f"in tenant {instance.tenant.name}: {e}"
            )
            raise OpenStackBackendError(e)
        tenant_groups = models.SecurityGroup.objects.filter(tenant=instance.tenant)

        remote_ids = set(g.id for g in remote_groups)
        local_ids = set(
            tenant_groups.filter(instances=instance)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )

        logger.info(
            f"Security group sync comparison for instance {instance.name}: "
            f"remote_ids={remote_ids}, local_ids={local_ids}, "
            f"to_remove={local_ids - remote_ids}, to_add={remote_ids - local_ids}"
        )

        # remove stale groups
        stale_groups = tenant_groups.filter(backend_id__in=(local_ids - remote_ids))
        if stale_groups.exists():
            stale_group_info = [
                {"name": sg.name, "uuid": str(sg.uuid), "backend_id": sg.backend_id}
                for sg in stale_groups
            ]
            logger.info(
                f"Removing {stale_groups.count()} stale security groups from instance {instance.name}: "
                f"{stale_group_info}. Remote groups returned by Nova: "
                f"{[{'id': g.id, 'name': getattr(g, 'name', 'unknown')} for g in remote_groups]}"
            )
        instance.security_groups.remove(*stale_groups)
        for security_group in stale_groups:
            event_logger.emit(
                "Removed security group %s from instance %s"
                % (security_group.name, instance.name),
                EventType.OPENSTACK_SECURITY_GROUP_REMOVED_LOCALLY,
                {"instance": instance, "security_group": security_group},
                scopes=[instance],
            )

        # add missing groups
        for group_id in remote_ids - local_ids:
            try:
                security_group = tenant_groups.get(backend_id=group_id)
            except models.SecurityGroup.DoesNotExist:
                logger.exception(
                    f"Security group with id {group_id} does not exist in database. "
                    f"Server ID: {server_id}"
                )
            else:
                instance.security_groups.add(security_group)
                event_logger.emit(
                    "Removed security group %s from instance %s"
                    % (security_group.name, instance.name),
                    EventType.OPENSTACK_SECURITY_GROUP_ADDED_LOCALLY,
                    {"instance": instance, "security_group": security_group},
                    scopes=[instance],
                )

    @log_backend_action()
    def push_instance_security_groups(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        server_id = instance.backend_id
        try:
            remote_ids = set(g.id for g in nova.servers.list_security_group(server_id))
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        group_map = {
            group.backend_id: group
            for group in models.SecurityGroup.objects.filter(
                tenant=instance.tenant
            ).exclude(backend_id="")
        }

        local_ids = set(
            models.SecurityGroup.objects.filter(instances=instance)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
            .distinct()
        )

        # remove stale groups
        for group_id in remote_ids - local_ids:
            try:
                nova.servers.remove_security_group(server_id, group_id)
            except nova_exceptions.ClientException:
                logger.exception(
                    "Failed to remove security group %s from instance %s",
                    group_id,
                    server_id,
                )
            else:
                logger.info(
                    "Removed security group %s from instance %s", group_id, server_id
                )
                local_group = group_map.get(group_id)
                event_logger.emit(
                    "Removed security group %s from instance %s"
                    % (local_group and local_group.name or group_id, instance.name),
                    EventType.OPENSTACK_SECURITY_GROUP_REMOVED_REMOTELY,
                    {"instance": instance, "security_group": local_group},
                    scopes=[instance],
                )

        # add missing groups
        for group_id in local_ids - remote_ids:
            try:
                nova.servers.add_security_group(server_id, group_id)
            except nova_exceptions.ClientException:
                logger.exception(
                    "Failed to add security group %s to instance %s",
                    group_id,
                    server_id,
                )
            else:
                logger.info(
                    "Added security group %s to instance %s", group_id, server_id
                )
                local_group = group_map.get(group_id)
                event_logger.emit(
                    "Added security group %s to instance %s"
                    % (local_group and local_group.name or group_id, instance.name),
                    EventType.OPENSTACK_SECURITY_GROUP_ADDED_REMOTELY,
                    {"instance": instance, "security_group": local_group},
                    scopes=[instance],
                )

    @log_backend_action()
    def delete_instance(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.delete(instance.backend_id)
        except nova_exceptions.NotFound:
            logger.info("OpenStack instance %s is already deleted", instance.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        instance.decrease_backend_quotas_usage()
        for volume in instance.volumes.all():
            volume.decrease_backend_quotas_usage()

    @log_backend_action("check is instance deleted")
    def is_instance_deleted(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.get(instance.backend_id)
            return False
        except nova_exceptions.NotFound:
            return True
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def start_instance(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.start(instance.backend_id)
        except nova_exceptions.ClientException as e:
            if e.code == 409 and "it is in vm_state active" in e.message:
                logger.info(
                    "OpenStack instance %s is already started", instance.backend_id
                )
                return
            raise OpenStackBackendError(e)

    @log_backend_action()
    def stop_instance(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.stop(instance.backend_id)
        except nova_exceptions.ClientException as e:
            if e.code == 409 and "it is in vm_state stopped" in e.message:
                logger.info(
                    "OpenStack instance %s is already stopped", instance.backend_id
                )
                return
            raise OpenStackBackendError(e)
        else:
            instance.start_time = None
            instance.save(update_fields=["start_time"])

    @log_backend_action()
    def restart_instance(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.reboot(instance.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def rescue_instance(self, instance: models.Instance, rescue_image_ref: str = None):
        """Put instance into Nova rescue mode.

        Volume-backed instances require a "stable device rescue" image
        (Glance hw_rescue_device or hw_rescue_bus property set) and an
        explicit rescue_image_ref — the legacy rescue path does not support
        BFV instances and Nova will leave the instance in unrecoverable
        ERROR state without one. Validation lives in the serializer; this
        method just calls the API.

        Note: novaclient's ``servers.rescue()`` takes ``image=`` (mapped to
        ``rescue_image_ref`` in the wire request body), not ``image_ref=``.
        Lab-validated.
        """
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.rescue(instance.backend_id, image=rescue_image_ref)
        except nova_exceptions.ClientException as e:
            if e.code == 409 and "vm_state rescued" in e.message:
                logger.info(
                    "OpenStack instance %s is already in rescue mode",
                    instance.backend_id,
                )
                return
            raise OpenStackBackendError(e)

    @log_backend_action()
    def unrescue_instance(self, instance: models.Instance):
        """Restore an instance from rescue mode."""
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.unrescue(instance.backend_id)
        except nova_exceptions.ClientException as e:
            if e.code == 409 and "vm_state active" in e.message:
                logger.info(
                    "OpenStack instance %s is already out of rescue mode",
                    instance.backend_id,
                )
                return
            raise OpenStackBackendError(e)

    @log_backend_action()
    def resize_instance(self, instance: models.Instance, flavor_id: str):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.resize(instance.backend_id, flavor_id, "MANUAL")
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def pull_instance_runtime_state(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            backend_instance = nova.servers.get(instance.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        if backend_instance.status != instance.runtime_state:
            instance.runtime_state = backend_instance.status
            instance.save(update_fields=["runtime_state"])

        if hasattr(backend_instance, "fault"):
            error_message = backend_instance.fault["message"]
            if instance.error_message != error_message:
                instance.error_message = error_message
                instance.save(update_fields=["error_message"])

    @log_backend_action()
    def confirm_instance_resize(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            nova.servers.confirm_resize(instance.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def get_console_url(self, instance: models.Instance):
        url = None
        service_settings = instance.tenant.service_settings
        console_type = service_settings.get_option("console_type")

        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            url = nova.servers.get_console_url(instance.backend_id, console_type)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        # newer API seems to return remote_console sometimes. According to spec it should be 'console'
        result_url = ""
        if "console" in url:
            result_url = url["console"]["url"]
        elif "remote_console" in url:
            result_url = url["remote_console"]["url"]

        console_domain_override = service_settings.get_option("console_domain_override")
        if console_domain_override:
            parsed_url = urlparse(result_url)
            if ":" in console_domain_override:
                # Override includes port (e.g. "lb.example.com:443")
                parsed_url = parsed_url._replace(netloc=console_domain_override)
            elif parsed_url.port:
                parsed_url = parsed_url._replace(
                    netloc=f"{console_domain_override}:{parsed_url.port}"
                )
            else:
                parsed_url = parsed_url._replace(netloc=console_domain_override)
            result_url = urlunparse(parsed_url)
        return result_url

    @log_backend_action()
    def get_console_output(self, instance: models.Instance, length=None):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        try:
            return nova.servers.get_console_output(instance.backend_id, length)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def pull_tenant_volume_availability_zones(self, tenant: models.Tenant):
        if not self.is_volume_availability_zone_supported():
            return

        session = get_tenant_session(tenant)
        try:
            cinder = get_cinder_client(session)
            backend_zones = cinder.availability_zones.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        self._pull_zones(tenant, backend_zones, models.VolumeAvailabilityZone)

    @reraise_exceptions
    @log_backend_action()
    def pull_tenant_network_rbac_policies(self, tenant: models.Tenant):
        """Pull network RBAC policies from OpenStack for a tenant."""
        # Use admin session to get full access to all RBAC policies
        neutron = get_neutron_client(self.admin_session)

        # Get all network RBAC policies from OpenStack
        backend_policies = neutron.list_rbac_policies(object_type="network")[
            "rbac_policies"
        ]

        # Process INCOMING network sharing policies - networks shared TO this tenant by other tenants
        # This should always run, even if tenant has no networks

        incoming_policies = [
            p for p in backend_policies if p["target_tenant"] == tenant.backend_id
        ]
        incoming_backend_ids = [p["id"] for p in incoming_policies]

        # Delete stale incoming policies BEFORE inserting fresh ones. When
        # OpenStack recreates an RBAC policy, the new entry has a new
        # ``backend_id`` but reuses the ``(network, target_tenant, policy_type)``
        # trio — which is the database's unique constraint. Cleaning up first
        # frees the trio so the subsequent insert does not violate the
        # constraint.
        stale_incoming_policies = models.NetworkRBACPolicy.objects.filter(
            target_tenant=tenant
        ).exclude(backend_id__in=incoming_backend_ids)
        stale_incoming_count = stale_incoming_policies.count()
        for policy in stale_incoming_policies:
            logger.info(
                "Deleting stale NetworkRBACPolicy: %s (network: %s, target: %s)",
                policy.backend_id,
                policy.network.name,
                policy.target_tenant.name,
            )
        stale_incoming_policies.delete()
        if stale_incoming_count:
            logger.info(
                "Deleted %d stale NetworkRBACPolicy objects", stale_incoming_count
            )

        for backend_policy in incoming_policies:
            network = models.Network.objects.filter(
                backend_id=backend_policy["object_id"]
            ).first()

            if not network:
                continue

            if network.tenant.service_settings != tenant.service_settings:
                # Skip policies whose source tenant exists in other service
                logger.debug(
                    "Skipping RBAC policy %s because source tenant exists in the other service %s",
                    str(network.tenant.service_settings),
                )
                event_logger.emit(
                    "RBAC policy %s skipped: source tenant %s from different service %s",
                    event_type=EventType.OPENSTACK_NETWORK_PULLED,
                    event_context={
                        "rbac_policy_id": backend_policy["id"],
                        "source_tenant": network.tenant,
                        "target_tenant": tenant,
                        "network": network,
                    },
                    scopes=[tenant, network.tenant],
                )
                continue

            # Create or update the RBAC policy by its backend_id (stable
            # identifier for a given OpenStack policy object). Stale rows that
            # would otherwise collide on the unique trio have already been
            # removed above.
            policy, created = models.NetworkRBACPolicy.objects.update_or_create(
                backend_id=backend_policy["id"],
                defaults={
                    "network": network,
                    "target_tenant": tenant,
                    "policy_type": backend_policy["action"],
                },
            )

            if created:
                logger.info(
                    "Created NetworkRBACPolicy from backend: %s (network: %s, source tenant: %s)",
                    backend_policy["id"],
                    network.name,
                    network.tenant.name,
                )

        # Process OUTGOING policies only if tenant has networks
        tenant_networks = tenant.networks.all()

        if not tenant_networks.exists():
            return

        # Get network backend IDs for filtering
        network_backend_ids = list(tenant_networks.values_list("backend_id", flat=True))

        # Filter policies that are for networks belonging to this tenant
        outgoing_policies = [
            p for p in backend_policies if p["object_id"] in network_backend_ids
        ]
        outgoing_backend_ids = [p["id"] for p in outgoing_policies]

        # Delete stale outgoing policies BEFORE inserting fresh ones, for the
        # same reason as the incoming branch above.
        stale_outgoing_policies = models.NetworkRBACPolicy.objects.filter(
            network__in=tenant_networks
        ).exclude(backend_id__in=outgoing_backend_ids)
        stale_outgoing_count = stale_outgoing_policies.count()
        for policy in stale_outgoing_policies:
            logger.info(
                "Deleting stale NetworkRBACPolicy: %s (network: %s, target: %s)",
                policy.backend_id,
                policy.network.name,
                policy.target_tenant.name,
            )
        stale_outgoing_policies.delete()
        if stale_outgoing_count:
            logger.info(
                "Deleted %d stale NetworkRBACPolicy objects", stale_outgoing_count
            )

        # Process each outgoing policy
        for backend_policy in outgoing_policies:
            network = tenant_networks.filter(
                backend_id=backend_policy["object_id"]
            ).first()

            if not network:
                continue

            try:
                target_tenant = models.Tenant.objects.get(
                    backend_id=backend_policy["target_tenant"],
                    service_settings=tenant.service_settings,
                )
            except models.Tenant.DoesNotExist:
                # Skip policies whose target tenant doesn't exist in Waldur
                logger.debug(
                    "Skipping RBAC policy %s because target tenant %s doesn't exist in Waldur",
                    backend_policy["id"],
                    backend_policy["target_tenant"],
                )
                continue

            # Create or update the RBAC policy (stale rows that would collide
            # on the unique trio have already been removed above).
            policy, created = models.NetworkRBACPolicy.objects.update_or_create(
                backend_id=backend_policy["id"],
                defaults={
                    "network": network,
                    "target_tenant": target_tenant,
                    "policy_type": backend_policy["action"],
                },
            )

            if created:
                logger.info(
                    "Created NetworkRBACPolicy from backend: %s (network: %s, target: %s)",
                    backend_policy["id"],
                    network.name,
                    target_tenant.name,
                )

    @reraise_exceptions
    def create_network_rbac_policy(
        self, network, target_tenant, policy_type="access_as_shared"
    ):
        neutron = get_neutron_client(self.admin_session)
        rbac_policy = {
            "rbac_policy": {
                "object_type": "network",
                "object_id": network.backend_id,
                "action": policy_type,
                "target_tenant": target_tenant.backend_id,
            }
        }
        response = neutron.create_rbac_policy(rbac_policy)
        return response.get("rbac_policy", {}).get("id")

    @reraise_exceptions
    def delete_network_rbac_policy(self, rbac_id):
        neutron = get_neutron_client(self.admin_session)
        neutron.delete_rbac_policy(rbac_id)

    @reraise_exceptions
    def enable_port_security(self, port):
        neutron = get_neutron_client(self.admin_session)
        neutron.update_port(port.backend_id, {"port": {"port_security_enabled": True}})
        logger.info(
            "Port security has been enabled for port %s (backend_id: %s).",
            port.uuid.hex,
            port.backend_id,
        )

    @reraise_exceptions
    def disable_port_security(self, port):
        neutron = get_neutron_client(self.admin_session)

        neutron.update_port(port.backend_id, {"port": {"security_groups": []}})
        logger.info(
            "Security groups have been removed from port %s (backend_id: %s).",
            port.uuid.hex,
            port.backend_id,
        )

        neutron.update_port(port.backend_id, {"port": {"port_security_enabled": False}})
        logger.info(
            "Port security has been disabled for port %s (backend_id: %s).",
            port.uuid.hex,
            port.backend_id,
        )

    @reraise_exceptions
    def enable_port(self, port):
        neutron = get_neutron_client(self.admin_session)
        neutron.update_port(port.backend_id, {"port": {"admin_state_up": True}})
        logger.info(
            "Port %s (backend_id: %s) has been enabled.",
            port.uuid.hex,
            port.backend_id,
        )

    @reraise_exceptions
    def disable_port(self, port):
        neutron = get_neutron_client(self.admin_session)
        neutron.update_port(port.backend_id, {"port": {"admin_state_up": False}})
        logger.info(
            "Port %s (backend_id: %s) has been disabled.",
            port.uuid.hex,
            port.backend_id,
        )

    @reraise_exceptions
    def update_port_ip(self, port, subnet_backend_id, ip_address):
        neutron = get_neutron_client(self.admin_session)
        neutron.update_port(
            port.backend_id,
            {
                "port": {
                    "fixed_ips": [
                        {
                            "subnet_id": subnet_backend_id,
                            "ip_address": ip_address,
                        }
                    ]
                }
            },
        )
        logger.info(
            "Port %s (backend_id: %s) IP changed to %s in subnet %s.",
            port.name or port.uuid.hex,
            port.backend_id,
            ip_address,
            subnet_backend_id,
        )

    def add_router_interface(self, router: models.Router, subnet=None, port=None):
        """
        Add an interface to a router. Either subnet or port must be provided.
        """
        neutron = get_neutron_client(self.admin_session)

        params = {}
        if subnet:
            params["subnet_id"] = subnet.backend_id
        if port:
            params["port_id"] = port.backend_id
        try:
            neutron.add_interface_router(router.backend_id, params)
        except Exception as e:
            raise OpenStackBackendError(
                f"Failed to add interface to router {router.backend_id}: {e}"
            )

    def remove_router_interface(self, router, subnet=None, port=None):
        """
        Remove an interface from a router. Either subnet or port must be provided.
        """
        neutron = get_neutron_client(self.admin_session)

        params = {}
        if subnet:
            params["subnet_id"] = subnet.backend_id
        if port:
            params["port_id"] = port.backend_id
        try:
            neutron.remove_interface_router(router.backend_id, params)
        except Exception as e:
            raise OpenStackBackendError(
                f"Failed to remove interface from router {router.backend_id}: {e}"
            )

    def remove_router_interface_safely(
        self, router: models.Router, subnet_id=None, port_id=None
    ):
        """Remove router interface handling case when port is already deleted."""
        old_routes = router.routes
        subnet = None
        port = None
        if subnet_id:
            subnet = models.SubNet.objects.get(id=subnet_id)
        if port_id:
            port = models.Port.objects.get(id=port_id)

        try:
            self.remove_router_interface(router, subnet, port)
        except OpenStackBackendError as e:
            raise OpenStackBackendError(
                f"Unable to remove a router interface: {e.args[0]}"
            )

        removed_interface = None
        if subnet:
            removed_interface = {"type": "subnet", "backend_id": subnet.backend_id}
        elif port:
            removed_interface = {"type": "port", "backend_id": port.backend_id}
        event_logger.emit(
            "Interface was removed from router.",
            event_type=EventType.OPENSTACK_ROUTER_UPDATED,
            event_context={
                "router": router,
                "old_routes": old_routes,
                "new_routes": old_routes,  # routes are not changed, but for consistency
                "tenant_backend_id": router.tenant.backend_id,
                "changed_interface": removed_interface,
            },
            scopes=[router, router.project, router.project.customer],
        )
        self.pull_tenant_routers(router.tenant, router.backend_id)
        self.pull_tenant_ports(router.tenant)

    def delete_router(self, router: models.Router):
        if not router.backend_id:
            logger.warning(
                "Cannot remove a router without backend_id: %s",
                router,
            )
            return
        neutron = get_neutron_client(self.admin_session)

        try:
            router_ports = neutron.list_ports(device_id=router.backend_id)

            for port in router_ports.get("ports", []):
                try:
                    neutron.delete_port(port["id"])
                except neutron_exceptions.NeutronClientException as e:
                    logger.warning(
                        "Failed to delete port %s for router %s: %s",
                        port["id"],
                        router.backend_id,
                        e,
                    )

            neutron.delete_router(router.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_load_balancer(self, load_balancer: models.LoadBalancer):
        get_octavia_client(load_balancer.tenant).create_load_balancer(load_balancer)
        logger.info(
            "Load balancer %s has been created in the backend.",
            load_balancer.name,
        )

    @log_backend_action()
    def delete_load_balancer(self, load_balancer: models.LoadBalancer):
        if not load_balancer.backend_id:
            logger.warning(
                "Cannot remove load balancer without backend_id: %s",
                load_balancer,
            )
            return

        get_octavia_client(load_balancer.tenant).delete_load_balancer(load_balancer)
        logger.info(
            "Load balancer %s has been deleted from the backend.",
            load_balancer.name,
        )

    @log_backend_action()
    def update_load_balancer(self, load_balancer: models.LoadBalancer):
        if not load_balancer.backend_id:
            logger.warning(
                "Cannot update load balancer without backend_id: %s",
                load_balancer,
            )
            return

        get_octavia_client(load_balancer.tenant).update_load_balancer(
            load_balancer, name=load_balancer.name
        )
        logger.info(
            "Load balancer %s has been updated in the backend.",
            load_balancer,
        )

    @log_backend_action()
    def pull_load_balancer(self, load_balancer: models.LoadBalancer):
        if not load_balancer.backend_id:
            return

        get_octavia_client(load_balancer.tenant).pull_load_balancer(load_balancer)

        logger.info(
            "Load balancer %s has been pulled in the backend.",
            load_balancer,
        )

    @log_backend_action()
    def attach_floating_ip_to_load_balancer_vip(
        self, load_balancer: models.LoadBalancer, serialized_floating_ip=None
    ):
        floating_ip: models.FloatingIP = core_utils.deserialize_instance(
            serialized_floating_ip
        )
        """Attach a floating IP to the load balancer VIP port."""

        if not load_balancer.vip_port or not load_balancer.vip_port.backend_id:
            raise OpenStackBackendError(
                "Load balancer VIP port is not available yet. "
                "Wait for the load balancer to become ACTIVE."
            )
        if load_balancer.tenant != floating_ip.tenant:
            raise OpenStackBackendError(
                "Floating IP must belong to the same tenant as the load balancer."
            )
        session = get_tenant_session(load_balancer.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.update_floatingip(
                floating_ip.backend_id,
                {"floatingip": {"port_id": load_balancer.vip_port.backend_id}},
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(str(e))
        # Clear previous LB attachment if this FIP was attached elsewhere
        models.LoadBalancer.objects.filter(attached_floating_ip=floating_ip).update(
            attached_floating_ip=None
        )
        # Clear FIP port (VIP port is not in our Port model)
        floating_ip.port = None
        floating_ip.save(update_fields=["port"])
        load_balancer.attached_floating_ip = floating_ip
        load_balancer.save(update_fields=["attached_floating_ip"])
        logger.info(
            "Floating IP %s attached to load balancer %s VIP.",
            floating_ip.address,
            load_balancer.name,
        )

    @log_backend_action()
    def detach_floating_ip_from_load_balancer_vip(
        self, load_balancer: models.LoadBalancer
    ):
        """Detach floating IP from the load balancer VIP port."""
        if not load_balancer.attached_floating_ip:
            raise OpenStackBackendError("Load balancer has no floating IP attached.")
        floating_ip = load_balancer.attached_floating_ip
        session = get_tenant_session(load_balancer.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.update_floatingip(
                floating_ip.backend_id,
                {"floatingip": {"port_id": None}},
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(str(e))
        load_balancer.attached_floating_ip = None
        load_balancer.save(update_fields=["attached_floating_ip"])
        logger.info(
            "Floating IP %s detached from load balancer %s VIP.",
            floating_ip.address,
            load_balancer.name,
        )

    @log_backend_action()
    def set_load_balancer_vip_security_groups(
        self, load_balancer: models.LoadBalancer, serialized_security_groups=None
    ):
        """Set security groups on the load balancer VIP port."""
        security_groups = [
            core_utils.deserialize_instance(sg) for sg in serialized_security_groups
        ]
        if not load_balancer.vip_port or not load_balancer.vip_port.backend_id:
            raise OpenStackBackendError("Load balancer VIP port is not available yet.")
        session = get_tenant_session(load_balancer.tenant)
        neutron = get_neutron_client(session)
        sg_backend_ids = [sg.backend_id for sg in security_groups if sg.backend_id]
        try:
            neutron.update_port(
                load_balancer.vip_port.backend_id,
                {"port": {"security_groups": sg_backend_ids}},
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(str(e))
        # Sync the local Port M2M with the new security groups
        load_balancer.vip_port.security_groups.set(security_groups)
        logger.info(
            "Security groups %s set on load balancer %s VIP port.",
            [sg.name for sg in security_groups],
            load_balancer.name,
        )

    def pull_tenant_load_balancers(self, tenant: models.Tenant):
        """Sync load balancers from Octavia for the tenant."""
        octavia_client = get_octavia_client(tenant)
        if not octavia_client.is_available():
            logger.info(
                "Octavia service is not available for tenant %s, skipping load balancer sync.",
                tenant,
            )
            return
        try:
            backend_load_balancers = octavia_client.get_tenant_load_balancers()
        except OpenStackBackendError as e:
            logger.warning("Failed to pull load balancers for tenant %s: %s", tenant, e)
            return
        for backend_load_balancer in backend_load_balancers:
            load_balancer = models.LoadBalancer.objects.filter(
                tenant=tenant,
                backend_id=backend_load_balancer.id,
            ).first()

            if load_balancer is not None:
                octavia_client._backend_load_balancer_to_load_balancer(
                    backend_load_balancer, load_balancer
                )
                continue

            vip_subnet_backend_id = backend_load_balancer.vip_subnet_id
            vip_subnet = models.SubNet.objects.filter(
                tenant=tenant, backend_id=vip_subnet_backend_id
            ).first()
            if vip_subnet is None:
                logger.warning(
                    "Skipping load balancer %r (backend_id=%s): VIP subnet %r "
                    "is not registered in Waldur for tenant %s.",
                    backend_load_balancer.name,
                    backend_load_balancer.id,
                    vip_subnet_backend_id,
                    tenant,
                )
                continue
            new_load_balancer = models.LoadBalancer.objects.create(
                tenant=tenant,
                name=backend_load_balancer.name,
                backend_id=backend_load_balancer.id,
                vip_subnet=vip_subnet,
                service_settings=tenant.service_settings,
                project=tenant.project,
                state=CoreStates.OK,
            )
            octavia_client._backend_load_balancer_to_load_balancer(
                backend_load_balancer, new_load_balancer
            )
        remote_ids = {str(lb.id) for lb in backend_load_balancers}
        stale = models.LoadBalancer.objects.filter(tenant=tenant).exclude(
            backend_id__in=remote_ids
        )
        stale.delete()

    @log_backend_action()
    def create_pool(self, pool: models.Pool):
        if not pool.load_balancer.backend_id:
            raise OpenStackBackendError(
                "Load balancer must be created in the backend before creating a pool."
            )
        get_octavia_client(pool.load_balancer.tenant).create_pool(pool)
        logger.info("Pool %s has been created in the backend.", pool.name)

    @log_backend_action()
    def delete_pool(self, pool: models.Pool):
        if not pool.backend_id:
            logger.warning("Cannot remove pool without backend_id: %s", pool)
            return
        get_octavia_client(pool.load_balancer.tenant).delete_pool(pool)
        logger.info("Pool %s has been deleted from the backend.", pool.name)

    @log_backend_action()
    def pull_pool(self, pool: models.Pool):
        if not pool.backend_id:
            return
        get_octavia_client(pool.load_balancer.tenant).pull_pool(pool)

    @log_backend_action()
    def update_pool(self, pool: models.Pool):
        if not pool.backend_id:
            return
        get_octavia_client(pool.load_balancer.tenant).update_pool(pool)
        logger.info("Pool %s has been updated in the backend.", pool.name)

    def pull_tenant_pools(self, tenant: models.Tenant):
        """Sync pools from Octavia for the tenant."""
        load_balancers = (
            models.LoadBalancer.objects.filter(tenant=tenant)
            .exclude(backend_id__isnull=True)
            .exclude(backend_id="")
        )
        if not load_balancers.exists():
            return
        octavia_client = get_octavia_client(tenant)
        if not octavia_client.is_available():
            return
        for lb in load_balancers:
            try:
                backend_pools = octavia_client.list_pools_for_load_balancer(
                    lb.backend_id
                )
            except OpenStackBackendError as e:
                logger.warning("Failed to pull pools for load balancer %s: %s", lb, e)
                continue
            for backend_pool in backend_pools:
                backend_id = str(backend_pool.id)
                defaults = {
                    "name": backend_pool.name or "",
                    "protocol": backend_pool.protocol or "TCP",
                    "lb_algorithm": backend_pool.lb_algorithm or "SOURCE_IP_PORT",
                    "provisioning_status": backend_pool.provisioning_status or "",
                    "operating_status": backend_pool.operating_status or "",
                    "service_settings": tenant.service_settings,
                    "project": tenant.project,
                    "state": CoreStates.OK,
                    "error_message": "",
                }
                try:
                    models.Pool.objects.update_or_create(
                        load_balancer=lb,
                        backend_id=backend_id,
                        defaults=defaults,
                    )
                except IntegrityError:
                    logger.warning(
                        "Could not create pool with backend ID %s "
                        "and load balancer %s due to concurrent update.",
                        backend_id,
                        lb,
                    )
            remote_ids = {str(p.id) for p in backend_pools}
            stale = models.Pool.objects.filter(load_balancer=lb).exclude(
                backend_id__in=remote_ids
            )
            stale.delete()

    @log_backend_action()
    def create_pool_member(self, member: models.PoolMember):
        if not member.pool.backend_id:
            raise OpenStackBackendError(
                "Pool must be created in the backend before creating a member."
            )
        if not member.subnet.backend_id:
            raise OpenStackBackendError(
                "Subnet must be created in the backend before creating a member."
            )
        get_octavia_client(member.pool.load_balancer.tenant).create_pool_member(member)
        logger.info("Pool member %s has been created in the backend.", member.name)

    @log_backend_action()
    def delete_pool_member(self, member: models.PoolMember):
        if not member.backend_id:
            logger.warning("Cannot remove pool member without backend_id: %s", member)
            return
        get_octavia_client(member.pool.load_balancer.tenant).delete_pool_member(member)
        logger.info("Pool member %s has been deleted from the backend.", member.name)

    @log_backend_action()
    def pull_pool_member(self, member: models.PoolMember):
        if not member.backend_id:
            return
        get_octavia_client(member.pool.load_balancer.tenant).pull_pool_member(member)

    @log_backend_action()
    def update_pool_member(self, member: models.PoolMember):
        if not member.backend_id:
            return
        get_octavia_client(member.pool.load_balancer.tenant).update_pool_member(member)
        logger.info("Pool member %s has been updated in the backend.", member.name)

    def pull_tenant_pool_members(self, tenant: models.Tenant):
        """Sync pool members from Octavia for the tenant."""
        pools = (
            models.Pool.objects.filter(
                load_balancer__tenant=tenant,
            )
            .exclude(backend_id__isnull=True)
            .exclude(backend_id="")
        )
        if not pools.exists():
            return
        octavia_client = get_octavia_client(tenant)
        if not octavia_client.is_available():
            return
        for pool in pools:
            try:
                backend_members = octavia_client.list_members_for_pool(pool.backend_id)
            except OpenStackBackendError as e:
                logger.warning("Failed to pull members for pool %s: %s", pool, e)
                continue
            for backend_member in backend_members:
                backend_id = str(backend_member.id)
                subnet_obj = None
                if getattr(backend_member, "subnet_id", None):
                    subnet_obj = models.SubNet.objects.filter(
                        tenant=tenant,
                        backend_id=backend_member.subnet_id,
                    ).first()
                defaults = {
                    "name": backend_member.name or "",
                    "address": backend_member.address or "0.0.0.0",
                    "protocol_port": backend_member.protocol_port or 80,
                    "subnet": subnet_obj,
                    "weight": backend_member.weight
                    if backend_member.weight is not None
                    else 1,
                    "provisioning_status": backend_member.provisioning_status or "",
                    "operating_status": backend_member.operating_status or "",
                    "service_settings": tenant.service_settings,
                    "project": tenant.project,
                    "state": CoreStates.OK,
                    "error_message": "",
                }
                try:
                    models.PoolMember.objects.update_or_create(
                        pool=pool,
                        backend_id=backend_id,
                        defaults=defaults,
                    )
                except IntegrityError:
                    logger.warning(
                        "Could not create pool member with backend ID %s "
                        "and pool %s due to concurrent update.",
                        backend_id,
                        pool,
                    )
            remote_ids = {str(m.id) for m in backend_members}
            stale = models.PoolMember.objects.filter(pool=pool).exclude(
                backend_id__in=remote_ids
            )
            stale.delete()

    @log_backend_action()
    def create_health_monitor(self, health_monitor: models.HealthMonitor):
        if not health_monitor.pool.backend_id:
            raise OpenStackBackendError(
                "Pool must be created in the backend before creating a health monitor."
            )
        get_octavia_client(
            health_monitor.pool.load_balancer.tenant
        ).create_health_monitor(health_monitor)
        logger.info(
            "Health monitor %s has been created in the backend.",
            health_monitor.name,
        )

    @log_backend_action()
    def delete_health_monitor(self, health_monitor: models.HealthMonitor):
        if not health_monitor.backend_id:
            logger.warning(
                "Cannot remove health monitor without backend_id: %s",
                health_monitor,
            )
            return
        get_octavia_client(
            health_monitor.pool.load_balancer.tenant
        ).delete_health_monitor(health_monitor)
        logger.info(
            "Health monitor %s has been deleted from the backend.",
            health_monitor.name,
        )

    @log_backend_action()
    def pull_health_monitor(self, health_monitor: models.HealthMonitor):
        if not health_monitor.backend_id:
            return
        get_octavia_client(
            health_monitor.pool.load_balancer.tenant
        ).pull_health_monitor(health_monitor)

    @log_backend_action()
    def update_health_monitor(self, health_monitor: models.HealthMonitor):
        if not health_monitor.backend_id:
            return
        get_octavia_client(
            health_monitor.pool.load_balancer.tenant
        ).update_health_monitor(health_monitor)
        logger.info(
            "Health monitor %s has been updated in the backend.",
            health_monitor.name,
        )

    def pull_tenant_healthmonitors(self, tenant: models.Tenant):
        """Sync health monitors from Octavia for the tenant."""
        pools = (
            models.Pool.objects.filter(
                load_balancer__tenant=tenant,
            )
            .exclude(backend_id__isnull=True)
            .exclude(backend_id="")
        )
        if not pools.exists():
            return
        octavia_client = get_octavia_client(tenant)
        if not octavia_client.is_available():
            return
        for pool in pools:
            try:
                backend_hms = octavia_client.list_health_monitors_for_pool(
                    pool.backend_id
                )
            except OpenStackBackendError as e:
                logger.warning(
                    "Failed to pull health monitors for pool %s: %s", pool, e
                )
                continue
            for backend_hm in backend_hms:
                backend_id = str(backend_hm.id)
                defaults = {
                    "name": backend_hm.name or "",
                    "monitor_type": backend_hm.type or "TCP",
                    "delay": backend_hm.delay if backend_hm.delay is not None else 10,
                    "timeout": backend_hm.timeout
                    if backend_hm.timeout is not None
                    else 5,
                    "max_retries": backend_hm.max_retries
                    if backend_hm.max_retries is not None
                    else 3,
                    "provisioning_status": backend_hm.provisioning_status or "",
                    "operating_status": backend_hm.operating_status or "",
                    "service_settings": tenant.service_settings,
                    "project": tenant.project,
                    "state": CoreStates.OK,
                    "error_message": "",
                }
                try:
                    models.HealthMonitor.objects.update_or_create(
                        pool=pool,
                        defaults={**defaults, "backend_id": backend_id},
                    )
                except IntegrityError:
                    logger.warning(
                        "Could not create health monitor with backend ID %s "
                        "and pool %s due to concurrent update.",
                        backend_id,
                        pool,
                    )
            remote_ids = {str(hm.id) for hm in backend_hms}
            stale = models.HealthMonitor.objects.filter(pool=pool).exclude(
                backend_id__in=remote_ids
            )
            stale.delete()

    @log_backend_action()
    def create_listener(self, listener: models.Listener):
        if not listener.load_balancer.backend_id:
            raise OpenStackBackendError(
                "Load balancer must be created in the backend before creating a listener."
            )
        get_octavia_client(listener.load_balancer.tenant).create_listener(listener)
        logger.info("Listener %s has been created in the backend.", listener.name)

    @log_backend_action()
    def delete_listener(self, listener: models.Listener):
        if not listener.backend_id:
            logger.warning("Cannot remove listener without backend_id: %s", listener)
            return
        get_octavia_client(listener.load_balancer.tenant).delete_listener(listener)
        logger.info("Listener %s has been deleted from the backend.", listener.name)

    @log_backend_action()
    def pull_listener(self, listener: models.Listener):
        if not listener.backend_id:
            return
        get_octavia_client(listener.load_balancer.tenant).pull_listener(listener)

    @log_backend_action()
    def update_listener(self, listener: models.Listener):
        if not listener.backend_id:
            return
        get_octavia_client(listener.load_balancer.tenant).update_listener(listener)
        logger.info("Listener %s has been updated in the backend.", listener.name)

    def pull_tenant_listeners(self, tenant: models.Tenant):
        """Sync listeners from Octavia for the tenant."""
        load_balancers = (
            models.LoadBalancer.objects.filter(tenant=tenant)
            .exclude(backend_id__isnull=True)
            .exclude(backend_id="")
        )
        if not load_balancers.exists():
            return
        octavia_client = get_octavia_client(tenant)
        if not octavia_client.is_available():
            return
        for lb in load_balancers:
            try:
                backend_listeners = octavia_client.list_listeners_for_load_balancer(
                    lb.backend_id
                )
            except OpenStackBackendError as e:
                logger.warning(
                    "Failed to pull listeners for load balancer %s: %s", lb, e
                )
                continue
            for backend_listener in backend_listeners:
                backend_id = str(backend_listener.id)
                default_pool_id = backend_listener.default_pool_id
                default_pool = None
                if default_pool_id:
                    default_pool = models.Pool.objects.filter(
                        load_balancer=lb, backend_id=default_pool_id
                    ).first()
                defaults = {
                    "name": backend_listener.name or "",
                    "protocol": backend_listener.protocol or "TCP",
                    "protocol_port": backend_listener.protocol_port or 80,
                    "default_pool": default_pool,
                    "provisioning_status": backend_listener.provisioning_status or "",
                    "operating_status": backend_listener.operating_status or "",
                    "service_settings": tenant.service_settings,
                    "project": tenant.project,
                    "state": CoreStates.OK,
                    "error_message": "",
                }
                try:
                    models.Listener.objects.update_or_create(
                        load_balancer=lb,
                        backend_id=backend_id,
                        defaults=defaults,
                    )
                except IntegrityError:
                    logger.warning(
                        "Could not create listener with backend ID %s "
                        "and load balancer %s due to concurrent update.",
                        backend_id,
                        lb,
                    )
            remote_ids = {str(lst.id) for lst in backend_listeners}
            stale = models.Listener.objects.filter(load_balancer=lb).exclude(
                backend_id__in=remote_ids
            )
            stale.delete()

    @log_backend_action()
    def push_port_security_groups(self, port: models.Port):
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)

        local_ids = set(
            models.SecurityGroup.objects.filter(ports=port)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )

        # Update security groups
        try:
            neutron.update_port(
                port.backend_id, {"port": {"security_groups": list(local_ids)}}
            )
            logger.info(
                "Updated security groups for port %s to %s",
                port.backend_id,
                list(local_ids),
            )
        except neutron_exceptions.NeutronClientException:
            logger.exception(
                "Failed to update security groups for port %s", port.backend_id
            )

    @reraise_exceptions
    def create_image(self, tenant: models.Tenant, image_metadata: dict):
        session = get_tenant_session(tenant)
        glance = get_glance_client(session)

        image = glance.images.create(**image_metadata)
        return image

    @reraise_exceptions
    def upload_image_data(self, tenant: models.Tenant, image_id: str, input_stream):
        session = get_tenant_session(tenant)

        auth_token = session.get_token()
        glance_url = session.get_endpoint(service_type="image")

        def stream_wsgi_input():
            buffer_size = 8192
            data = input_stream.read(buffer_size)
            while data:
                yield data
                data = input_stream.read(buffer_size)

        upload_url = f"{glance_url}/v2/images/{image_id}/file"
        logger.debug(f"Starting upload to {upload_url}")

        try:
            # Resolve verification the same way the session above does:
            # get_verify_ssl applies DEFAULTS and returns a CA file path when a
            # client certificate is configured, which options.get() skipped.
            with httpx.Client(verify=get_verify_ssl(tenant.service_settings)) as client:
                upload_response = client.put(
                    upload_url,
                    content=stream_wsgi_input(),
                    headers={
                        "X-Auth-Token": auth_token,
                        "Content-Type": "application/octet-stream",
                    },
                )
        except httpx.RequestError as exc:
            logger.error(f"HTTPX request failed: {exc}")
            raise OpenStackBackendError(f"HTTPX request failed: {exc}")
        except Exception as exc:
            logger.error(f"Unexpected error during image upload: {exc}")
            raise OpenStackBackendError(f"Unexpected error during image upload: {exc}")

        if upload_response.status_code == 204:
            logger.info("Upload completed successfully")
            glance = get_glance_client(session)
            try:
                glance.images.get(image_id)
                return {
                    "status": "success",
                    "response": upload_response.text,
                }
            except glance_exceptions.HTTPNotFound:
                raise OpenStackBackendError(
                    f"Verification failed: {upload_response.text}"
                )
        else:
            logger.error(
                f"Upload failed with status code {upload_response.status_code}"
            )
            raise OpenStackBackendError(f"Failed: {upload_response.text}")

    @reraise_exceptions
    def get_image_count_total(self, tenant: models.Tenant) -> int:
        session = get_tenant_session(tenant)
        glance = get_glance_client(session)
        return len(list(glance.images.list()))

    @reraise_exceptions
    def get_image_size_total(self, tenant: models.Tenant) -> int:
        session = get_tenant_session(tenant)
        glance = get_glance_client(session)
        return sum([i.size or 0 for i in glance.images.list()])

    def get_free_ip(self, subnet: models.SubNet):
        neutron = get_neutron_client(self.admin_session)
        used_ips = set()
        ports = neutron.list_ports(fixed_ips=f"subnet_id={subnet.backend_id}")["ports"]
        for port in ports:
            for ip in port["fixed_ips"]:
                used_ips.add(ip["ip_address"])

        for pool in subnet.allocation_pools:
            start = ipaddress.IPv4Address(pool["start"])
            end = ipaddress.IPv4Address(pool["end"])
            for ip_int in range(int(start), int(end) + 1):
                ip = str(ipaddress.IPv4Address(ip_int))
                if ip not in used_ips:
                    return ip
        return None
