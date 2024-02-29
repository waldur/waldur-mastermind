import logging
import re

from cinderclient import exceptions as cinder_exceptions
from django.db import transaction
from glanceclient import exc as glance_exceptions
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions
from requests import ConnectionError

from waldur_core.structure.backend import ServiceBackend
from waldur_openstack.openstack_base.exceptions import OpenStackBackendError
from waldur_openstack.openstack_base.session import (
    get_cinder_client,
    get_glance_client,
    get_keystone_client,
    get_keystone_session,
    get_neutron_client,
    get_nova_client,
)

logger = logging.getLogger(__name__)

VALID_VOLUME_TYPE_NAME_PATTERN = re.compile(r"^gigabytes_[a-z]+[-_a-z]+$")


def is_valid_volume_type_name(name):
    return re.match(VALID_VOLUME_TYPE_NAME_PATTERN, name)


class BaseOpenStackBackend(ServiceBackend):
    def __init__(self, settings, tenant_id=None):
        self.settings = settings
        self.tenant_id = tenant_id

    @property
    def session(self):
        return get_keystone_session(self.settings, self.tenant_id)

    def ping(self, raise_exception=False):
        try:
            get_keystone_client(self.session)
        except keystone_exceptions.ClientException as e:
            if raise_exception:
                raise OpenStackBackendError(e)
            return False
        else:
            return True

    def ping_resource(self, instance):
        nova = get_nova_client(self.session)
        try:
            nova.servers.get(instance.backend_id)
        except (ConnectionError, nova_exceptions.ClientException):
            return False
        else:
            return True

    def _pull_tenant_quotas(self, backend_id, scope):
        # Cinder volumes and snapshots manager does not implement filtering by tenant_id.
        # Therefore we need to assume that tenant_id field is set up in backend settings.
        backend = BaseOpenStackBackend(self.settings, backend_id)
        for quota_name, limit in backend.get_tenant_quotas_limits(backend_id).items():
            scope.set_quota_limit(quota_name, limit)
        for quota_name, usage in backend.get_tenant_quotas_usage(backend_id).items():
            scope.set_quota_usage(quota_name, usage)

    def get_tenant_quotas_limits(self, tenant_backend_id):
        nova = get_nova_client(self.session)
        neutron = get_neutron_client(self.session)
        cinder = get_cinder_client(self.session)

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

    def get_tenant_quotas_usage(self, tenant_backend_id):
        nova = get_nova_client(self.session)
        neutron = get_neutron_client(self.session)
        cinder = get_cinder_client(self.session)

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

    def _log_security_group_rule_imported(self, rule):
        pass

    def _log_security_group_rule_pulled(self, rule):
        pass

    def _log_security_group_rule_cleaned(self, rule):
        pass

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

    def _get_current_properties(self, model):
        return {p.backend_id: p for p in model.objects.filter(settings=self.settings)}

    def _pull_images(self, model_class, filter_function=None):
        glance = get_glance_client(self.session)
        try:
            images = glance.images.list()
        except glance_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        images = [image for image in images if not image["status"] == "deleted"]
        if filter_function:
            images = list(filter(filter_function, images))

        with transaction.atomic():
            cur_images = self._get_current_properties(model_class)
            for backend_image in images:
                cur_images.pop(backend_image["id"], None)
                model_class.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_image["id"],
                    defaults={
                        "name": backend_image["name"],
                        "min_ram": backend_image["min_ram"],
                        "min_disk": self.gb2mb(backend_image["min_disk"]),
                    },
                )
            model_class.objects.filter(
                backend_id__in=cur_images.keys(), settings=self.settings
            ).delete()

    def _delete_backend_floating_ip(self, backend_id, tenant_backend_id):
        neutron = get_neutron_client(self.session)
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

    def _get_current_volume_types(self):
        """
        It is expected that this method is implemented in inherited backend classes
        so that it would be possible to avoid circular dependency between base and openstack_tenant
        applications.
        """
        return []
