import functools
import logging
import re
from urllib.parse import urlparse, urlunparse

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
from waldur_core.core.utils import create_batch_fetcher, pwgen
from waldur_core.structure.backend import ServiceBackend, log_backend_action
from waldur_core.structure.registry import get_resource_type
from waldur_core.structure.signals import resource_pulled
from waldur_core.structure.utils import (
    handle_resource_not_found,
    handle_resource_update_success,
    update_pulled_fields,
)
from waldur_openstack.exceptions import (
    OpenStackBackendError,
    OpenStackTenantNotFound,
)
from waldur_openstack.session import (
    get_cinder_client,
    get_glance_client,
    get_keystone_client,
    get_keystone_session,
    get_neutron_client,
    get_nova_client,
)
from waldur_openstack.utils import is_valid_volume_type_name

from . import models, signals
from .log import event_logger

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
        ) as e:
            instance = args[0]

            if isinstance(instance, core_models.ErrorMessageMixin):
                instance.error_message = str(e)
                instance.save(update_fields=["error_message"])

            raise OpenStackBackendError(e)

    return wrapped


class OpenStackBackend(ServiceBackend):
    DEFAULTS = {
        "tenant_name": "admin",
        "console_type": "novnc",
        "verify_ssl": False,
    }

    def __init__(self, settings):
        self.settings = settings

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

    def pull_resources(self):
        self.pull_tenants()

    def pull_tenants(self):
        keystone = get_keystone_client(self.admin_session)

        try:
            backend_tenants = keystone.projects.list(domain=self._get_domain())
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        backend_tenants_mapping = {tenant.id: tenant for tenant in backend_tenants}

        tenants = models.Tenant.objects.filter(
            state__in=[models.Tenant.States.OK, models.Tenant.States.ERRED],
            service_settings=self.settings,
        )
        for tenant in tenants:
            backend_tenant = backend_tenants_mapping.get(tenant.backend_id)
            if backend_tenant is None:
                handle_resource_not_found(tenant)
                signals.tenant_does_not_exist_in_backend.send(
                    models.Tenant, instance=tenant
                )
                continue

            imported_backend_tenant = models.Tenant(
                name=backend_tenant.name,
                description=backend_tenant.description,
                backend_id=backend_tenant.id,
                state=models.Tenant.States.OK,
            )
            update_pulled_fields(
                tenant, imported_backend_tenant, models.Tenant.get_backend_fields()
            )
            handle_resource_update_success(tenant)

    def _get_domain(self):
        """Get current domain"""
        keystone = get_keystone_client(self.admin_session)
        return keystone.domains.find(name=self.settings.domain or "Default")

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
            local_image, _ = models.Image.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_image["id"],
                defaults={
                    "name": remote_image["name"]
                    or remote_image.get("description")
                    or remote_image["id"],
                    "min_ram": remote_image["min_ram"],
                    "min_disk": self.gb2mb(remote_image["min_disk"]),
                },
            )
            tenant.images.add(local_image)

        existing_image_ids = remote_image_ids & local_image_ids
        for image_backend_id in existing_image_ids:
            remote_image = remote_image_mapping[image_backend_id]
            local_image, _ = models.Image.objects.update_or_create(
                settings=self.settings,
                backend_id=remote_image["id"],
                defaults={
                    "name": remote_image["name"]
                    or remote_image.get("description")
                    or remote_image["id"],
                    "min_ram": remote_image["min_ram"],
                    "min_disk": self.gb2mb(remote_image["min_disk"]),
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
            state__in=[models.FloatingIP.States.OK, models.FloatingIP.States.ERRED],
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
                models.FloatingIP.States.OK,
                models.FloatingIP.States.ERRED,
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
            state=models.FloatingIP.States.OK,
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
        if backend_default_group and local_default_group:
            local_default_group.backend_id = backend_default_group["id"]
            local_default_group.save(update_fields=["backend_id"])
            self.push_security_group_rules(local_default_group)
            local_default_group.set_ok()
            local_default_group.save()
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
                models.SecurityGroup.States.OK,
                models.SecurityGroup.States.ERRED,
            ],
        ).exclude(backend_id__in=remote_ids)

        logger.info(f"Removing {stale_groups.count()} sec groups from {tenants}.")
        for security_group in stale_groups:
            event_logger.openstack_security_group.info(
                "Security group %s has been cleaned from cache." % security_group.name,
                event_type="openstack_security_group_cleaned",
                event_context={
                    "security_group": security_group,
                },
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
                    models.SecurityGroup.States.OK,
                    models.SecurityGroup.States.ERRED,
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
        event_logger.openstack_security_group.info(
            "Security group %s has been imported to local cache." % security_group.name,
            event_type="openstack_security_group_imported",
            event_context={"security_group": security_group},
        )

    def _log_security_group_pulled(self, security_group: models.SecurityGroup):
        event_logger.openstack_security_group.info(
            "Security group %s has been pulled from backend." % security_group.name,
            event_type="openstack_security_group_pulled",
            event_context={"security_group": security_group},
        )

    def _log_security_group_rule_imported(self, rule):
        event_logger.openstack_security_group_rule.info(
            "Security group rule %s has been imported from backend." % str(rule),
            event_type="openstack_security_group_rule_imported",
            event_context={"security_group_rule": rule},
        )

    def _log_security_group_rule_pulled(self, rule):
        logger.debug("Security group rule %s has been pulled from backend.", str(rule))

    def _log_security_group_rule_cleaned(self, rule):
        event_logger.openstack_security_group_rule.info(
            "Security group rule %s has been cleaned from cache." % str(rule),
            event_type="openstack_security_group_rule_cleaned",
            event_context={"security_group_rule": rule},
        )

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
            state=models.SecurityGroup.States.OK,
        )

        for field, value in kwargs.items():
            setattr(security_group, field, value)

        return security_group

    def pull_tenant_routers(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
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
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

            defaults = {
                "name": backend_router["name"],
                "description": backend_router["description"],
                "routes": backend_router["routes"],
                "fixed_ips": fixed_ips,
                "service_settings": tenant.service_settings,
                "project": tenant.project,
                "state": models.Router.States.OK,
            }
            try:
                models.Router.objects.update_or_create(
                    tenant=tenant, backend_id=backend_id, defaults=defaults
                )
            except IntegrityError:
                logger.warning(
                    "Could not create router with backend ID %s "
                    "and tenant %s due to concurrent update.",
                    backend_id,
                    tenant,
                )

        remote_ids = {ip["id"] for ip in backend_routers}
        stale_routers = models.Router.objects.filter(tenant=tenant).exclude(
            backend_id__in=remote_ids
        )
        stale_routers.delete()

    def _tenant_mappings(self, queryset):
        rows = queryset.exclude(backend_id="").values("id", "backend_id")
        return {row["backend_id"]: row["id"] for row in rows}

    def pull_tenant_ports(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)

        try:
            backend_ports = neutron.list_ports(tenant_id=tenant.backend_id)["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        network_mapping = self._tenant_mappings(
            models.Network.objects.filter(tenant=tenant)
        )
        subnet_mapping = self._tenant_mappings(
            models.SubNet.objects.filter(tenant=tenant)
        )
        security_group_mapping = self._tenant_mappings(
            models.SecurityGroup.objects.filter(tenant=tenant)
        )
        instance_mapping = self._tenant_mappings(
            models.Instance.objects.filter(tenant=tenant)
        )

        for backend_port in backend_ports:
            backend_id = backend_port["id"]

            try:
                subnet_backend_id = backend_port["fixed_ips"][0]["subnet_id"]
            except (AttributeError, KeyError):
                pass

            device_id = backend_port.get("device_id")
            instance_id = instance_mapping.get(device_id)

            defaults = {
                "name": backend_port["name"],
                "description": backend_port["description"],
                "service_settings": tenant.service_settings,
                "project": tenant.project,
                "instance_id": instance_id,
                "subnet_id": subnet_mapping.get(subnet_backend_id),
                "state": models.Port.States.OK,
                "mac_address": backend_port["mac_address"],
                "fixed_ips": backend_port["fixed_ips"],
                "allowed_address_pairs": backend_port.get("allowed_address_pairs", []),
                "network_id": network_mapping.get(backend_port["network_id"]),
                "device_id": device_id,
                "device_owner": backend_port.get("device_owner"),
                "port_security_enabled": backend_port.get(
                    "port_security_enabled", True
                ),
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
            except IntegrityError:
                logger.warning(
                    "Could not create or update port with backend ID %s "
                    "and tenant %s due to concurrent update.",
                    backend_id,
                    tenant,
                )

        remote_ids = {ip["id"] for ip in backend_ports}
        stale_ports = (
            models.Port.objects.filter(tenant=tenant)
            .exclude(backend_id="")
            .exclude(backend_id__in=remote_ids)
        )
        stale_ports.delete()

    def pull_tenant_networks(self, tenant: models.Tenant):
        self._pull_networks([tenant])

    def _pull_networks(self, tenants):
        tenant_mappings = {tenant.backend_id: tenant for tenant in tenants}
        backend_networks = self.list_networks(list(tenant_mappings.keys()))

        networks = []
        with transaction.atomic():
            for backend_network in backend_networks:
                tenant = tenant_mappings.get(backend_network["tenant_id"])
                if not tenant:
                    logger.debug(
                        "Skipping network %s synchronization because its tenant %s is not available.",
                        backend_network["id"],
                        backend_network["tenant_id"],
                    )
                    continue

                imported_network = self._backend_network_to_network(
                    backend_network,
                    tenant=tenant,
                    service_settings=tenant.service_settings,
                    project=tenant.project,
                )

                try:
                    network = tenant.networks.get(
                        backend_id=imported_network.backend_id
                    )
                except models.Network.DoesNotExist:
                    imported_network.save()
                    network = imported_network

                    event_logger.openstack_network.info(
                        "Network %s has been imported to local cache." % network.name,
                        event_type="openstack_network_imported",
                        event_context={
                            "network": network,
                        },
                    )
                else:
                    modified = update_pulled_fields(
                        network, imported_network, models.Network.get_backend_fields()
                    )
                    handle_resource_update_success(network)
                    if modified:
                        event_logger.openstack_network.info(
                            "Network %s has been pulled from backend." % network.name,
                            event_type="openstack_network_pulled",
                            event_context={
                                "network": network,
                            },
                        )
                networks.append(network)

            networks_uuid = [network_item.uuid for network_item in networks]
            stale_networks = models.Network.objects.filter(
                state__in=[models.Network.States.OK, models.Network.States.ERRED],
                tenant__in=tenants,
            ).exclude(uuid__in=networks_uuid)
            for network in stale_networks:
                event_logger.openstack_network.info(
                    "Network %s has been cleaned from cache." % network.name,
                    event_type="openstack_network_cleaned",
                    event_context={
                        "network": network,
                    },
                )
            stale_networks.delete()

        return networks

    @method_decorator(create_batch_fetcher)
    def list_networks(self, tenants):
        neutron = get_neutron_client(self.admin_session)
        try:
            return neutron.list_networks(tenant_id=tenants)["networks"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    def _backend_network_to_network(self, backend_network, **kwargs):
        network = models.Network(
            name=backend_network["name"],
            description=backend_network["description"],
            is_external=backend_network["router:external"],
            runtime_state=backend_network["status"],
            mtu=backend_network.get("mtu"),
            backend_id=backend_network["id"],
            state=models.Network.States.OK,
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

    def pull_subnets(self, tenant: models.Tenant = None, network=None):
        neutron = get_neutron_client(self.admin_session)

        if tenant:
            networks = tenant.networks.all()
        elif network:
            networks = [network]
        else:
            networks = models.Network.objects.filter(
                state=models.Network.States.OK,
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

                    event_logger.openstack_subnet.info(
                        "SubNet %s has been imported to local cache." % subnet.name,
                        event_type="openstack_subnet_imported",
                        event_context={
                            "subnet": subnet,
                        },
                    )

                else:
                    modified = update_pulled_fields(
                        subnet, imported_subnet, models.SubNet.get_backend_fields()
                    )
                    handle_resource_update_success(subnet)
                    if modified:
                        event_logger.openstack_subnet.info(
                            "SubNet %s has been pulled from backend." % subnet.name,
                            event_type="openstack_subnet_pulled",
                            event_context={
                                "subnet": subnet,
                            },
                        )

                subnet_uuids.append(subnet.uuid)

            stale_subnets = models.SubNet.objects.filter(
                state__in=[models.SubNet.States.OK, models.SubNet.States.ERRED],
                network__in=networks,
            ).exclude(uuid__in=subnet_uuids)
            for subnet in stale_subnets:
                event_logger.openstack_subnet.info(
                    "SubNet %s has been cleaned." % subnet.name,
                    event_type="openstack_subnet_cleaned",
                    event_context={
                        "subnet": subnet,
                    },
                )
            stale_subnets.delete()

    def _backend_subnet_to_subnet(self, backend_subnet, **kwargs):
        subnet = models.SubNet(
            name=backend_subnet["name"],
            description=backend_subnet["description"],
            allocation_pools=backend_subnet["allocation_pools"],
            cidr=backend_subnet["cidr"],
            ip_version=backend_subnet["ip_version"],
            enable_dhcp=backend_subnet["enable_dhcp"],
            gateway_ip=backend_subnet.get("gateway_ip"),
            dns_nameservers=backend_subnet["dns_nameservers"],
            host_routes=sorted(
                backend_subnet.get("host_routes", []), key=lambda x: tuple(x.values())
            ),
            backend_id=backend_subnet["id"],
            state=models.SubNet.States.OK,
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
            tenant.state = models.Tenant.States.OK
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
                backend_rule = backend_rules[backend_rule_id]
                security_group_rule = models.SecurityGroupRule(
                    security_group=security_group,
                    backend_id=backend_rule_id,
                    **self._import_security_group_rule(backend_rule),
                )
                event_logger.openstack_security_group_rule.info(
                    "Extra security group rule %s has been deleted in "
                    "backend because it is not defined in Waldur."
                    % str(security_group_rule),
                    event_type="openstack_security_group_rule_deleted",
                    event_context={"security_group_rule": security_group_rule},
                )

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
                event_logger.openstack_security_group_rule.info(
                    "Security group rule %s has been deleted "
                    "from backend because it has different params." % str(local_rule),
                    event_type="openstack_security_group_rule_deleted",
                    event_context={"security_group_rule": local_rule},
                )

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
                event_logger.openstack_security_group_rule.info(
                    "Security group rule %s has been created in backend."
                    % str(local_rule),
                    event_type="openstack_security_group_rule_created",
                    event_context={"security_group_rule": local_rule},
                )

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

            event_logger.openstack_security_group.info(
                'Security group "%s" has been created in the backend.'
                % security_group.name,
                event_type="openstack_security_group_created",
                event_context={
                    "security_group": security_group,
                },
            )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_security_group(self, security_group: models.SecurityGroup):
        session = get_tenant_session(security_group.tenant)
        neutron = get_neutron_client(session)
        try:
            neutron.delete_security_group(security_group.backend_id)

            event_logger.openstack_security_group.info(
                'Security group "%s" has been deleted' % security_group.name,
                event_type="openstack_security_group_deleted",
                event_context={
                    "security_group": security_group,
                },
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

            event_logger.openstack_security_group.info(
                'Security group "%s" has been updated' % security_group.name,
                event_type="openstack_security_group_updated",
                event_context={
                    "security_group": security_group,
                },
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_server_group(self, server_group: models.ServerGroup):
        session = get_tenant_session(server_group.tenant)
        nova = get_nova_client(session)
        try:
            backend_server_group = nova.server_groups.create(
                name=server_group.name, policies=server_group.policy
            )
            server_group.backend_id = backend_server_group.id
            server_group.save(update_fields=["backend_id"])
            event_logger.openstack_server_group.info(
                'Server group "%s" has been created in the backend.'
                % server_group.name,
                event_type="openstack_server_group_created",
                event_context={
                    "server_group": server_group,
                },
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_server_group(self, server_group: models.ServerGroup):
        session = get_tenant_session(server_group.tenant)
        nova = get_nova_client(session)
        try:
            nova.server_groups.delete(server_group.backend_id)
            event_logger.openstack_server_group.info(
                'Server group "%s" has been deleted' % server_group.name,
                event_type="openstack_server_group_deleted",
                event_context={
                    "server_group": server_group,
                },
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
    def detect_external_network(self, tenant: models.Tenant):
        session = get_tenant_session(tenant)
        neutron = get_neutron_client(session)
        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)["routers"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        if bool(routers):
            router = routers[0]
        else:
            logger.warning(
                "Tenant %s (PK: %s) does not have connected routers.", tenant, tenant.pk
            )
            return

        ext_gw = router.get("external_gateway_info", {})
        if ext_gw and "network_id" in ext_gw:
            tenant.external_network_id = ext_gw["network_id"]
            tenant.save()
            logger.info(
                "Found and set external network with id %s for tenant %s (PK: %s)",
                ext_gw["network_id"],
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
            response = neutron.create_network({"networks": [data]})
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            backend_network = response["networks"][0]
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

            event_logger.openstack_network.info(
                "Network %s has been created in the backend." % network.name,
                event_type="openstack_network_created",
                event_context={
                    "network": network,
                },
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
        self._update_network(network, {"name": network.name})
        event_logger.openstack_network.info(
            "Network name %s has been updated." % network.name,
            event_type="openstack_network_updated",
            event_context={"network": network},
        )

    @log_backend_action()
    def set_network_mtu(self, network: models.Network):
        self._update_network(network, {"mtu": network.mtu})
        event_logger.openstack_network.info(
            "Network MTU %s has been updated." % network.name,
            event_type="openstack_network_updated",
            event_context={"network": network},
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
            event_logger.openstack_network.info(
                "Network %s has been deleted" % network.name,
                event_type="openstack_network_deleted",
                event_context={
                    "network": network,
                },
            )

    @log_backend_action()
    def import_tenant_networks(self, tenant: models.Tenant):
        networks = self._pull_networks([tenant])
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
                event_logger.openstack_network.info(
                    "Network %s has been pulled from backend." % network.name,
                    event_type="openstack_network_pulled",
                    event_context={"network": network},
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
            "allocation_pools": subnet.allocation_pools,
            "ip_version": subnet.ip_version,
            "enable_dhcp": subnet.enable_dhcp,
        }
        if subnet.dns_nameservers:
            data["dns_nameservers"] = subnet.dns_nameservers
        if subnet.host_routes:
            data["host_routes"] = subnet.host_routes
        if subnet.disable_gateway:
            data["gateway_ip"] = None
        elif subnet.gateway_ip:
            data["gateway_ip"] = subnet.gateway_ip
        try:
            response = neutron.create_subnet({"subnets": [data]})
            backend_subnet = response["subnets"][0]
            subnet.backend_id = backend_subnet["id"]
            if backend_subnet.get("gateway_ip"):
                subnet.gateway_ip = backend_subnet["gateway_ip"]

            # Automatically create router for subnet
            self.connect_subnet(subnet)
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            subnet.save()

            event_logger.openstack_subnet.info(
                "SubNet %s has been created in the backend." % subnet.name,
                event_type="openstack_subnet_created",
                event_context={
                    "subnet": subnet,
                },
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

        neutron.update_subnet(subnet.backend_id, {"subnet": data})
        event_logger.openstack_subnet.info(
            "SubNet %s has been updated" % subnet.name,
            event_type="openstack_subnet_updated",
            event_context={
                "subnet": subnet,
            },
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

            event_logger.openstack_subnet.info(
                "SubNet %s has been disconnected from network" % subnet.name,
                event_type="openstack_subnet_updated",
                event_context={
                    "subnet": subnet,
                },
            )

    def connect_subnet(self, subnet: models.SubNet):
        self.connect_router(
            subnet.network.tenant,
            subnet.network.name,
            subnet.backend_id,
            network_id=subnet.network.backend_id,
        )
        subnet.is_connected = True
        subnet.save(update_fields=["is_connected"])

        event_logger.openstack_subnet.info(
            "SubNet %s has been connected to network" % subnet.name,
            event_type="openstack_subnet_updated",
            event_context={
                "subnet": subnet,
            },
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
            event_logger.openstack_subnet.info(
                "SubNet %s has been deleted" % subnet.name,
                event_type="openstack_subnet_deleted",
                event_context={
                    "subnet": subnet,
                },
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
                event_logger.openstack_subnet.info(
                    "SubNet %s has been pulled from backend." % subnet.name,
                    event_type="openstack_subnet_pulled",
                    event_context={
                        "subnet": subnet,
                    },
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

            event_logger.openstack_floating_ip.info(
                f"The description of the floating IP [{floating_ip}] has been changed to [{description}].",
                event_type="openstack_floating_ip_description_updated",
                event_context={
                    "floating_ip": floating_ip,
                },
            )

    @log_backend_action("create floating ip")
    def create_floating_ip(self, floating_ip: models.FloatingIP):
        session = get_tenant_session(floating_ip.tenant)
        neutron = get_neutron_client(session)
        try:
            backend_floating_ip = neutron.create_floatingip(
                {
                    "floatingip": {
                        "floating_network_id": floating_ip.tenant.external_network_id,
                        "tenant_id": floating_ip.tenant.backend_id,
                    }
                }
            )["floatingip"]
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
                    external_ip_info = backend_router["external_gateway_info"][
                        "external_fixed_ips"
                    ][0]
                    logger.info(
                        "External network %s has been connected to the router %s with external IP %s within subnet %s.",
                        network_id,
                        router["name"],
                        external_ip_info["ip_address"],
                        external_ip_info["subnet_id"],
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

    def pull_service_settings_quotas(self):
        nova = get_nova_client(self.admin_session)
        try:
            stats = nova.hypervisor_stats.statistics()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        self.settings.set_quota_limit("openstack_vcpu", stats.vcpus)
        self.settings.set_quota_usage("openstack_vcpu", stats.vcpus_used)

        self.settings.set_quota_limit("openstack_ram", stats.memory_mb)
        self.settings.set_quota_usage("openstack_ram", stats.memory_mb_used)

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
    def create_port(self, port: models.Port, serialized_network: models.Network):
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)
        network = core_utils.deserialize_instance(serialized_network)

        port_payload = {
            "name": port.name,
            "description": port.description,
            "network_id": network.backend_id,
            "fixed_ips": port.fixed_ips,
            "tenant_id": port.tenant.backend_id,
        }
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
            port.save(update_fields=["backend_id", "mac_address", "fixed_ips"])

            event_logger.opentask_port.info(
                f"Port [{port}] has been created in the backend for network [{network}].",
                event_type="openstack_port_created",
                event_context={"port": port},
            )

            return port

    @log_backend_action()
    def delete_port(self, port: models.Port):
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)

        try:
            neutron.delete_port(port.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            event_logger.openstack_port.info(
                f"Port [{port}] has been deleted from network [{port.network}].",
                event_type="openstack_port_deleted",
                event_context={"port": port},
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

            event_logger.openstack_floating_ip.info(
                f"Floating IP [{floating_ip}] has been attached to port [{port}].",
                event_type="openstack_floating_ip_attached",
                event_context={
                    "floating_ip": floating_ip,
                    "port": port,
                },
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

            event_logger.openstack_floating_ip.info(
                f"Floating IP {floating_ip} has been detached from port {port}.",
                event_type="openstack_floating_ip_detached",
                event_context={
                    "floating_ip": floating_ip,
                    "port": port,
                },
            )

    def _log_server_group_imported(self, server_group: models.ServerGroup):
        event_logger.openstack_server_group.info(
            "Server group %s has been imported to local cache." % server_group.name,
            event_type="openstack_server_group_imported",
            event_context={"server_group": server_group},
        )

    def _log_server_group_pulled(self, server_group: models.ServerGroup):
        event_logger.openstack_server_group.info(
            "Server group %s has been pulled from backend." % server_group.name,
            event_type="openstack_server_group_pulled",
            event_context={"server_group": server_group},
        )

    def _log_server_group_created(self, server_group: models.ServerGroup):
        event_logger.openstack_server_group.info(
            'Server group "%s" has been created in the backend.' % server_group.name,
            event_type="openstack_server_group_created",
            event_context={"server_group": server_group},
        )

    def _backend_server_group_to_server_group(self, backend_server_group, **kwargs):
        server_group = models.ServerGroup(
            name=backend_server_group.name,
            policy=backend_server_group.policies[0],
            backend_id=backend_server_group.id,
            state=models.ServerGroup.States.OK,
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
                    models.ServerGroup.States.OK,
                    models.ServerGroup.States.ERRED,
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
                models.ServerGroup.States.OK,
                models.ServerGroup.States.ERRED,
            ],
        ).exclude(backend_id__in=remote_ids)
        for server_group in stale_groups:
            event_logger.openstack_server_group.info(
                "Server group %s has been cleaned from cache." % server_group.name,
                event_type="openstack_server_group_cleaned",
                event_context={
                    "server_group": server_group,
                },
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

        local_port = models.Port(
            backend_id=remote_port["id"],
            mac_address=remote_port["mac_address"],
            fixed_ips=fixed_ips,
            allowed_address_pairs=remote_port.get("allowed_address_pairs", []),
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
            state__in=[
                models.Volume.States.OK,
                models.Volume.States.ERRED,
            ],
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
                models.Snapshot.States.OK,
                models.Snapshot.States.ERRED,
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
        backend_instances = self.get_instances(tenant)
        instances = models.Instance.objects.filter(
            tenant=tenant,
            state__in=[
                models.Instance.States.OK,
                models.Instance.States.ERRED,
            ],
        )
        backend_instances_map = {
            backend_instance.backend_id: backend_instance
            for backend_instance in backend_instances
        }
        for instance in instances:
            try:
                backend_instance = backend_instances_map[instance.backend_id]
            except KeyError:
                handle_resource_not_found(instance)
            else:
                self.update_instance_fields(instance, backend_instance)
                # XXX: can be optimized after https://goo.gl/BZKo8Y will be resolved.
                self.pull_instance_security_groups(instance)
                handle_resource_update_success(instance)

    def update_instance_fields(self, instance: models.Instance, backend_instance):
        # Preserve flavor fields in Waldur database if flavor is deleted in OpenStack
        fields = set(models.Instance.get_backend_fields())
        flavor_fields = {"flavor_name", "flavor_disk", "ram", "cores", "disk"}
        if not backend_instance.flavor_name:
            fields = fields - flavor_fields
        fields = list(fields)

        update_pulled_fields(instance, backend_instance, fields)

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
            backend_volume = cinder.volumes.create(**kwargs)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.backend_id = backend_volume.id
        if hasattr(backend_volume, "volume_image_metadata"):
            volume.image_metadata = backend_volume.volume_image_metadata
        volume.bootable = backend_volume.bootable == "true"
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
        cinder = get_cinder_client(session)
        try:
            cinder.volumes.extend(volume.backend_id, self.mb2gb(volume.size))
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def import_volume(self, tenant: models.Tenant, backend_id, project=None, save=True):
        """Restore Waldur volume instance based on backend data."""
        session = get_tenant_session(tenant)
        cinder = get_cinder_client(session)
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
                    name=backend_volume_availability_zone, tenant=tenant
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
            state=models.Volume.States.OK,
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
            state=models.Snapshot.States.OK,
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
            backend_flavor = nova.flavors.get(backend_flavor_id)

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

            nics = [
                {"port-id": port.backend_id}
                for port in instance.ports.all()
                if port.backend_id
            ]

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

            block_device_mapping_v2 = []
            for volume in instance.volumes.iterator():
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

            if (
                instance.tenant.service_settings.options.get("config_drive", False)
                is True
            ):
                server_create_parameters["config_drive"] = True

            if server_group is not None:
                server_create_parameters["scheduler_hints"] = {"group": server_group}

            server = nova.servers.create(**server_create_parameters)
            instance.backend_id = server.id
            instance.save()
        except nova_exceptions.ClientException as e:
            logger.exception("Failed to provision instance %s", instance.uuid)
            raise OpenStackBackendError(e)
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
                elif floating_ip.state == models.FloatingIP.States.OK:
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
                instance.floating_ips.filter(state=models.FloatingIP.States.OK)
                .exclude(backend_id="")
                .values_list("backend_id", flat=True)
            )
            stale_ids = frontend_ids - backend_ids
            if stale_ids:
                logger.info("About to detach floating IPs from ports: %s", stale_ids)
                instance.floating_ips.filter(backend_id__in=stale_ids).update(port=None)

    @log_backend_action()
    def push_instance_floating_ips(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        neutron = get_neutron_client(session)
        instance_floating_ips = instance.floating_ips
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
                    event_logger.openstack_tenant_floating_ip.info(
                        f"Floating IP {floating_ip.address} has been disconnected from instance {instance.name}.",
                        event_type="openstack_floating_ip_disconnected",
                        event_context={
                            "floating_ip": floating_ip,
                            "instance": instance,
                        },
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
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)
                else:
                    event_logger.openstack_tenant_floating_ip.info(
                        f"Floating IP {floating_ip.address} has been connected to instance {instance.name}.",
                        event_type="openstack_floating_ip_connected",
                        event_context={
                            "floating_ip": floating_ip,
                            "instance": instance,
                        },
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

        session = get_tenant_session(tenant)
        nova = get_nova_client(session)

        try:
            backend_instance = nova.servers.get(backend_id)
            attached_volume_ids = [
                v.volumeId for v in nova.volumes.get_server_volumes(backend_id)
            ]
            flavor_id = backend_instance.flavor["id"]
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        instance: models.Instance = self._backend_instance_to_instance(
            tenant, backend_instance, flavor_id, connected_internal_network_names
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
                volumes.append(
                    self.import_volume(tenant, backend_volume_id, project, save)
                )
        return volumes

    def _backend_instance_to_instance(
        self,
        tenant: models.Tenant,
        backend_instance,
        backend_flavor_id=None,
        connected_internal_network_names=None,
    ):
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
                launch_time = timezone.make_aware(d, timezone.utc)

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
        backend_networks = backend_instance.networks
        external_backend_networks = (
            set(backend_networks.keys()) - connected_internal_network_names
        )
        external_backend_ips = [
            ",".join(backend_networks[ext_net]) for ext_net in external_backend_networks
        ]

        instance = models.Instance(
            name=backend_instance.name or backend_instance.id,
            key_name=backend_instance.key_name or "",
            start_time=launch_time,
            state=models.Instance.States.OK,
            runtime_state=backend_instance.status,
            created=dateparse.parse_datetime(backend_instance.created),
            backend_id=backend_instance.id,
            availability_zone=availability_zone,
            hypervisor_hostname=hypervisor_hostname,
            directly_connected_ips=",".join(external_backend_ips),
        )

        if backend_flavor_id:
            try:
                flavor = models.Flavor.objects.get(
                    settings=tenant.service_settings, backend_id=backend_flavor_id
                )
                instance.flavor_name = flavor.name
                instance.flavor_disk = flavor.disk
                instance.cores = flavor.cores
                instance.ram = flavor.ram
            except models.Flavor.DoesNotExist:
                backend_flavor = self._get_flavor(tenant, backend_flavor_id)
                # If flavor has been removed in OpenStack cloud, we should skip update
                if backend_flavor:
                    instance.flavor_name = backend_flavor.name
                    instance.flavor_disk = self.gb2mb(backend_flavor.disk)
                    instance.cores = backend_flavor.vcpus
                    instance.ram = backend_flavor.ram

        attached_volumes = backend_instance.to_dict().get(
            "os-extended-volumes:volumes_attached", []
        )
        attached_volume_ids = [volume["id"] for volume in attached_volumes]
        volumes = self._import_instance_volumes(
            tenant, attached_volume_ids, project=None, save=False
        )
        instance.disk = sum(volume.size for volume in volumes)

        return instance

    def _get_flavor(self, tenant: models.Tenant, flavor_id):
        session = get_tenant_session(tenant)
        nova = get_nova_client(session)
        try:
            return nova.flavors.get(flavor_id)
        except nova_exceptions.NotFound:
            logger.info("OpenStack flavor %s is gone.", flavor_id)
            return None
        except nova_exceptions.ClientException as e:
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
            flavor_id = backend_instance.flavor["id"]
            instances.append(
                self._backend_instance_to_instance(tenant, backend_instance, flavor_id)
            )
        return instances

    def get_importable_instances(self, tenant: models.Tenant) -> list[models.Instance]:
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
        front_zones_map = {
            zone.name: zone for zone in frontend_model.objects.filter(tenant=tenant)
        }

        back_zones_map = {
            zone.zoneName: zone.zoneState.get("available", True)
            for zone in backend_zones
            if zone.zoneName != default_zone
        }

        missing_zones = set(back_zones_map.keys()) - set(front_zones_map.keys())
        for zone in missing_zones:
            frontend_model.objects.create(
                settings=self.settings,
                tenant=tenant,
                name=zone,
            )

        stale_zones = set(front_zones_map.keys()) - set(back_zones_map.keys())
        frontend_model.objects.filter(name__in=stale_zones, tenant=tenant).delete()

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

        subnets = models.SubNet.objects.filter(tenant=instance.tenant)
        subnet_mappings = {subnet.backend_id: subnet for subnet in subnets}

        with transaction.atomic():
            for backend_port in backend_ports:
                imported_port = self.parse_backend_port(backend_port, instance=instance)
                subnet = subnet_mappings.get(imported_port._subnet_backend_id)
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
                    # Update backend ID for pending port
                    update_pulled_fields(
                        port,
                        imported_port,
                        models.Port.get_backend_fields() + ("backend_id",),
                    )

                elif imported_port.backend_id in existing_ips:
                    port = existing_ips[imported_port.backend_id]
                    update_pulled_fields(
                        port,
                        imported_port,
                        models.Port.get_backend_fields(),
                    )

                elif imported_port.backend_id in local_ips:
                    port = local_ips[imported_port.backend_id]
                    if port.instance != instance:
                        logger.info(
                            "About to reassign shared port from instance %s to instance %s",
                            port.instance,
                            instance,
                        )
                        port.instance = instance
                        port.save()

                else:
                    logger.debug(
                        "About to create port. Instance ID: %s, subnet ID: %s",
                        instance.backend_id,
                        subnet.backend_id,
                    )
                    port = imported_port
                    port.subnet = subnet
                    port.project = subnet.project
                    port.tenant = subnet.tenant
                    port.network = subnet.network
                    port.service_settings = subnet.service_settings
                    port.instance = instance
                    port.save()

            # remove stale ports
            frontend_ids = set(existing_ips.keys())
            backend_ids = {port["id"] for port in backend_ports}
            stale_ids = frontend_ids - backend_ids
            if stale_ids:
                logger.info("About to delete ports with IDs %s", stale_ids)
                instance.ports.filter(backend_id__in=stale_ids).delete()

    @log_backend_action()
    def push_instance_ports(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        # we assume that port subnet cannot be changed
        neutron = get_neutron_client(session)
        nova = get_nova_client(session)

        try:
            backend_ports = neutron.list_ports(device_id=instance.backend_id)["ports"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        # delete stale ports
        exist_ids = instance.ports.values_list("backend_id", flat=True)
        for backend_port in backend_ports:
            if backend_port["id"] not in exist_ids:
                try:
                    logger.info(
                        "About to delete network port with ID %s.",
                        backend_port["id"],
                    )
                    neutron.delete_port(backend_port["id"])
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)

        # create new ports
        new_ports = instance.ports.exclude(
            backend_id__in=[ip["id"] for ip in backend_ports]
        )
        for new_port in new_ports:
            port_payload = {
                "network_id": new_port.subnet.network.backend_id,
                # If you specify only a subnet ID, OpenStack Networking
                # allocates an available IP from that subnet to the port.
                "fixed_ips": [
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
            try:
                logger.debug(
                    "About to create network port for instance %s in subnet %s.",
                    instance.backend_id,
                    new_port.subnet.backend_id,
                )
                backend_port = neutron.create_port({"port": port_payload})["port"]
                nova.servers.interface_attach(
                    instance.backend_id, backend_port["id"], None, None
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)
            except nova_exceptions.ClientException as e:
                raise OpenStackBackendError(e)
            new_port.mac_address = backend_port["mac_address"]
            new_port.fixed_ips = backend_port["fixed_ips"]
            new_port.backend_id = backend_port["id"]
            new_port.save()

    @log_backend_action()
    def create_instance_ports(self, instance: models.Instance):
        security_groups = list(
            instance.security_groups.values_list("backend_id", flat=True)
        )
        for port in instance.ports.all():
            self.create_instance_port(port, security_groups)

    def create_instance_port(self, port: models.Port, security_groups):
        session = get_tenant_session(port.tenant)
        neutron = get_neutron_client(session)

        logger.debug(
            "About to create network port. Network ID: %s. Subnet ID: %s.",
            port.subnet.network.backend_id,
            port.subnet.backend_id,
        )

        port_payload = {
            "network_id": port.subnet.network.backend_id,
            "fixed_ips": [
                {
                    "subnet_id": port.subnet.backend_id,
                }
            ],
            "security_groups": security_groups,
        }
        try:
            backend_port = neutron.create_port({"port": port_payload})["port"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        port.mac_address = backend_port["mac_address"]
        port.fixed_ips = backend_port["fixed_ips"]
        port.backend_id = backend_port["id"]
        port.save()

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
    def pull_instance_security_groups(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        server_id = instance.backend_id
        try:
            remote_groups = nova.servers.list_security_group(server_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        tenant_groups = models.SecurityGroup.objects.filter(tenant=instance.tenant)

        remote_ids = set(g.id for g in remote_groups)
        local_ids = set(
            tenant_groups.filter(instances=instance)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )

        # remove stale groups
        stale_groups = tenant_groups.filter(backend_id__in=(local_ids - remote_ids))
        instance.security_groups.remove(*stale_groups)

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

    @log_backend_action()
    def push_instance_security_groups(self, instance: models.Instance):
        session = get_tenant_session(instance.tenant)
        nova = get_nova_client(session)
        server_id = instance.backend_id
        try:
            remote_ids = set(g.id for g in nova.servers.list_security_group(server_id))
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        local_ids = set(
            models.SecurityGroup.objects.filter(instances=instance)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
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
            if parsed_url.port:
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
