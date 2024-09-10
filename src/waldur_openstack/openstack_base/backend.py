import logging

from cinderclient import exceptions as cinder_exceptions
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions
from requests import ConnectionError

from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure.backend import ServiceBackend
from waldur_openstack.openstack.utils import is_valid_volume_type_name
from waldur_openstack.openstack_base.exceptions import OpenStackBackendError
from waldur_openstack.openstack_base.session import (
    get_cinder_client,
    get_keystone_client,
    get_keystone_session,
    get_neutron_client,
    get_nova_client,
)

logger = logging.getLogger(__name__)


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

    def _pull_tenant_quotas(self, backend_id, scope: QuotaModelMixin):
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
