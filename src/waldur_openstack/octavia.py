"""
Octavia Load Balancer API client.
"""

import logging

import openstack as openstack_sdk
from openstack import exceptions as openstack_exceptions

from waldur_openstack import models
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.session import get_credentials

logger = logging.getLogger(__name__)


def get_octavia_client(tenant):
    return OctaviaClient(tenant)


class OctaviaClient:
    def __init__(self, tenant):
        self.tenant = tenant
        self._connection = None
        self._lb_proxy = None

    def _get_connection(self):
        if not self._connection:
            # Use admin credentials from service_settings scoped to the tenant project.
            # Do NOT pass tenant= to get_credentials() — that switches to per-tenant
            # user credentials which may lack the policy rights for Octavia operations.
            # We use get_credentials() + openstack_sdk.connect(**credentials) rather than
            # get_keystone_session() because openstack_sdk.connect(session=ks_session)
            # does not accept a keystoneauth1 Session directly and raises OptionError.
            credentials = get_credentials(self.tenant.service_settings)
            credentials.pop("project_name", None)
            credentials.pop("project_domain_name", None)
            credentials["project_id"] = self.tenant.backend_id
            auth_type = credentials.pop("auth_type", "password")
            if auth_type == "v3applicationcredential":
                self._connection = openstack_sdk.connect(
                    auth_url=credentials["auth_url"],
                    auth_type="v3applicationcredential",
                    application_credential_id=credentials["username"],
                    application_credential_secret=credentials["password"],
                )
            else:
                self._connection = openstack_sdk.connect(**credentials)
        return self._connection

    @property
    def connection(self):
        if not self._lb_proxy:
            if not self.is_available():
                raise OpenStackBackendError(
                    "Octavia load-balancer service is not available "
                    "for tenant %s." % self.tenant
                )
            self._lb_proxy = self._get_connection().load_balancer
        return self._lb_proxy

    def is_available(self):
        """Check whether the Octavia (load-balancer) service is in the catalog."""
        try:
            conn = self._get_connection()
            return conn.has_service("load-balancer")
        except Exception as e:
            logger.warning(
                "Failed to check Octavia availability for tenant %s: %s",
                self.tenant,
                e,
            )
            return False

    def create_load_balancer(self, load_balancer: models.LoadBalancer):
        backend_load_balancer = self.connection.create_load_balancer(
            name=load_balancer.name,
            vip_subnet_id=load_balancer.vip_subnet.backend_id,
            provider=load_balancer.provider or "ovn",
        )
        backend_load_balancer = self.connection.wait_for_load_balancer(
            backend_load_balancer.id
        )
        self._backend_load_balancer_to_load_balancer(
            backend_load_balancer, load_balancer
        )

    def delete_load_balancer(self, load_balancer: models.LoadBalancer):
        self.connection.delete_load_balancer(load_balancer.backend_id)

    def update_load_balancer(self, load_balancer: models.LoadBalancer, **kwargs):
        self.connection.update_load_balancer(load_balancer.backend_id, **kwargs)
        self.connection.wait_for_load_balancer(load_balancer.backend_id)
        self.pull_load_balancer(load_balancer)

    def pull_load_balancer(self, load_balancer: models.LoadBalancer, **kwargs):
        try:
            backend_load_balancer = self.connection.get_load_balancer(
                load_balancer.backend_id
            )
        except openstack_exceptions.NotFoundException:
            load_balancer.error_message = "Load balancer not found in backend"
            load_balancer.save(update_fields=["error_message"])
            return

        self._backend_load_balancer_to_load_balancer(
            backend_load_balancer, load_balancer
        )

    def get_tenant_load_balancers(self):
        return list(self.connection.load_balancers(project_id=self.tenant.backend_id))

    def _backend_load_balancer_to_load_balancer(
        self, backend_load_balancer, load_balancer
    ):
        load_balancer.backend_id = backend_load_balancer.id
        load_balancer.vip_address = backend_load_balancer.vip_address
        neutron_vip_port_backend_id = (
            getattr(backend_load_balancer, "vip_port_id", None) or None
        )
        if neutron_vip_port_backend_id:
            port = models.Port.objects.filter(
                tenant=load_balancer.tenant,
                backend_id=neutron_vip_port_backend_id,
            ).first()
            if port is None:
                from waldur_openstack.backend import OpenStackBackend

                port = OpenStackBackend(
                    load_balancer.tenant.service_settings
                ).import_port_from_neutron_by_id(
                    load_balancer.tenant, neutron_vip_port_backend_id
                )
            load_balancer.vip_port = port
            load_balancer.attached_floating_ip = (
                models.FloatingIP.objects.filter(
                    tenant=load_balancer.tenant,
                    port=port,
                )
                .order_by("pk")
                .first()
            )
        else:
            load_balancer.vip_port = None
            load_balancer.attached_floating_ip = None
        load_balancer.provisioning_status = backend_load_balancer.provisioning_status
        load_balancer.operating_status = backend_load_balancer.operating_status
        load_balancer.provider = backend_load_balancer.provider
        load_balancer.error_message = ""
        load_balancer.error_traceback = ""
        load_balancer.save(
            update_fields=[
                "backend_id",
                "vip_address",
                "vip_port",
                "attached_floating_ip",
                "provisioning_status",
                "operating_status",
                "provider",
                "error_message",
                "error_traceback",
            ]
        )

    # --- Pool ---

    def create_pool(self, pool: models.Pool):
        load_balancer_backend_id = pool.load_balancer.backend_id
        backend_pool = self.connection.create_pool(
            loadbalancer_id=load_balancer_backend_id,
            name=pool.name,
            protocol=pool.protocol,
            lb_algorithm=pool.lb_algorithm or "SOURCE_IP_PORT",
        )
        pool.backend_id = backend_pool.id
        pool.save()
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_pool(pool)

    def delete_pool(self, pool: models.Pool):
        load_balancer_backend_id = pool.load_balancer.backend_id
        self.connection.delete_pool(pool.backend_id)
        self.connection.wait_for_load_balancer(load_balancer_backend_id)

    def update_pool(self, pool: models.Pool):
        load_balancer_backend_id = pool.load_balancer.backend_id
        self.connection.update_pool(pool.backend_id, name=pool.name)
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_pool(pool)

    def pull_pool(self, pool: models.Pool):
        if not pool.backend_id:
            return
        try:
            backend_pool = self.connection.get_pool(pool.backend_id)
        except openstack_exceptions.NotFoundException:
            pool.error_message = "Pool not found in backend"
            pool.save(update_fields=["error_message"])
            return
        self._backend_pool_to_pool(backend_pool, pool)

    def list_pools_for_load_balancer(self, load_balancer_backend_id: str):
        return list(self.connection.pools(loadbalancer_id=load_balancer_backend_id))

    def _backend_pool_to_pool(self, backend_pool, pool: models.Pool):
        pool.backend_id = backend_pool.id
        pool.provisioning_status = backend_pool.provisioning_status or ""
        pool.operating_status = backend_pool.operating_status or ""
        pool.protocol = backend_pool.protocol or pool.protocol
        pool.lb_algorithm = backend_pool.lb_algorithm or pool.lb_algorithm
        pool.error_message = ""
        pool.error_traceback = ""
        pool.save(
            update_fields=[
                "backend_id",
                "provisioning_status",
                "operating_status",
                "protocol",
                "lb_algorithm",
                "error_message",
                "error_traceback",
            ]
        )

    # --- Listener ---

    def create_listener(self, listener: models.Listener):
        load_balancer_backend_id = listener.load_balancer.backend_id
        kwargs = {
            "loadbalancer_id": load_balancer_backend_id,
            "protocol": listener.protocol,
            "protocol_port": listener.protocol_port,
            "name": listener.name,
        }
        if listener.default_pool_id and listener.default_pool.backend_id:
            kwargs["default_pool_id"] = listener.default_pool.backend_id
        backend_listener = self.connection.create_listener(**kwargs)
        listener.backend_id = backend_listener.id
        listener.save()
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_listener(listener)

    def delete_listener(self, listener: models.Listener):
        load_balancer_backend_id = listener.load_balancer.backend_id
        self.connection.delete_listener(listener.backend_id)
        self.connection.wait_for_load_balancer(load_balancer_backend_id)

    def update_listener(self, listener: models.Listener):
        load_balancer_backend_id = listener.load_balancer.backend_id
        update_kwargs = {"name": listener.name}
        if listener.default_pool_id and listener.default_pool.backend_id:
            update_kwargs["default_pool_id"] = listener.default_pool.backend_id
        else:
            update_kwargs["default_pool_id"] = None
        self.connection.update_listener(listener.backend_id, **update_kwargs)
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_listener(listener)

    def pull_listener(self, listener: models.Listener):
        if not listener.backend_id:
            return
        try:
            backend_listener = self.connection.get_listener(listener.backend_id)
        except openstack_exceptions.NotFoundException:
            listener.error_message = "Listener not found in backend"
            listener.save(update_fields=["error_message"])
            return
        self._backend_listener_to_listener(backend_listener, listener)

    def list_listeners_for_load_balancer(self, load_balancer_backend_id: str):
        return list(
            self.connection.listeners(load_balancer_id=load_balancer_backend_id)
        )

    def _backend_listener_to_listener(
        self, backend_listener, listener: models.Listener
    ):
        listener.backend_id = backend_listener.id
        listener.provisioning_status = backend_listener.provisioning_status or ""
        listener.operating_status = backend_listener.operating_status or ""
        listener.protocol = backend_listener.protocol or listener.protocol
        listener.protocol_port = (
            backend_listener.protocol_port or listener.protocol_port
        )
        default_pool_id = backend_listener.default_pool_id
        if default_pool_id:
            default_pool = models.Pool.objects.filter(
                load_balancer=listener.load_balancer,
                backend_id=default_pool_id,
            ).first()
            listener.default_pool = default_pool
        else:
            listener.default_pool = None
        listener.error_message = ""
        listener.error_traceback = ""
        listener.save(
            update_fields=[
                "backend_id",
                "provisioning_status",
                "operating_status",
                "protocol",
                "protocol_port",
                "default_pool",
                "error_message",
                "error_traceback",
            ]
        )

    # --- Pool member ---

    def create_pool_member(self, member: models.PoolMember):
        load_balancer_backend_id = member.pool.load_balancer.backend_id
        backend_member = self.connection.create_member(
            member.pool.backend_id,
            address=str(member.address),
            protocol_port=member.protocol_port,
            subnet_id=member.subnet.backend_id,
            name=member.name or "",
            weight=member.weight,
        )
        member.backend_id = backend_member.id
        member.save()
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_pool_member(member)

    def delete_pool_member(self, member: models.PoolMember):
        load_balancer_backend_id = member.pool.load_balancer.backend_id
        self.connection.delete_member(member.backend_id, member.pool.backend_id)
        self.connection.wait_for_load_balancer(load_balancer_backend_id)

    def update_pool_member(self, member: models.PoolMember):
        load_balancer_backend_id = member.pool.load_balancer.backend_id
        self.connection.update_member(
            member.backend_id,
            member.pool.backend_id,
            name=member.name,
            weight=member.weight,
        )
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_pool_member(member)

    def pull_pool_member(self, member: models.PoolMember):
        if not member.backend_id:
            return
        try:
            backend_member = self.connection.get_member(
                member.backend_id, member.pool.backend_id
            )
        except openstack_exceptions.NotFoundException:
            member.error_message = "Member not found in backend"
            member.save(update_fields=["error_message"])
            return
        self._backend_pool_member_to_pool_member(backend_member, member)

    def list_members_for_pool(self, pool_backend_id: str):
        return list(self.connection.members(pool_backend_id))

    def _backend_pool_member_to_pool_member(
        self, backend_member, member: models.PoolMember
    ):
        member.backend_id = backend_member.id
        member.name = backend_member.name
        member.provisioning_status = backend_member.provisioning_status or ""
        member.operating_status = backend_member.operating_status or ""
        member.address = backend_member.address or member.address
        member.protocol_port = backend_member.protocol_port or member.protocol_port
        if backend_member.weight is not None:
            member.weight = backend_member.weight
        member.error_message = ""
        member.error_traceback = ""
        member.save(
            update_fields=[
                "name",
                "backend_id",
                "provisioning_status",
                "operating_status",
                "address",
                "protocol_port",
                "weight",
                "error_message",
                "error_traceback",
            ]
        )

    # --- Health monitor ---

    def create_health_monitor(self, health_monitor: models.HealthMonitor):
        load_balancer_backend_id = health_monitor.pool.load_balancer.backend_id
        backend_health_monitor = self.connection.create_health_monitor(
            pool_id=health_monitor.pool.backend_id,
            type=health_monitor.monitor_type,
            delay=health_monitor.delay,
            timeout=health_monitor.timeout,
            max_retries=health_monitor.max_retries,
            max_retries_down=health_monitor.max_retries_down,
            name=health_monitor.name or "",
        )
        health_monitor.backend_id = backend_health_monitor.id
        health_monitor.save()
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_health_monitor(health_monitor)

    def delete_health_monitor(self, health_monitor: models.HealthMonitor):
        load_balancer_backend_id = health_monitor.pool.load_balancer.backend_id
        self.connection.delete_health_monitor(health_monitor.backend_id)
        self.connection.wait_for_load_balancer(load_balancer_backend_id)

    def update_health_monitor(self, health_monitor: models.HealthMonitor):
        load_balancer_backend_id = health_monitor.pool.load_balancer.backend_id
        self.connection.update_health_monitor(
            health_monitor.backend_id,
            name=health_monitor.name,
            delay=health_monitor.delay,
            timeout=health_monitor.timeout,
            max_retries=health_monitor.max_retries,
            max_retries_down=health_monitor.max_retries_down,
        )
        self.connection.wait_for_load_balancer(load_balancer_backend_id)
        self.pull_health_monitor(health_monitor)

    def pull_health_monitor(self, health_monitor: models.HealthMonitor):
        if not health_monitor.backend_id:
            return
        try:
            backend_health_monitor = self.connection.get_health_monitor(
                health_monitor.backend_id
            )
        except openstack_exceptions.NotFoundException:
            health_monitor.error_message = "Health monitor not found in backend"
            health_monitor.save(update_fields=["error_message"])
            return
        self._backend_health_monitor_to_health_monitor(
            backend_health_monitor, health_monitor
        )

    def list_health_monitors_for_pool(self, pool_backend_id: str):
        return list(self.connection.health_monitors(pool_id=pool_backend_id))

    def _backend_health_monitor_to_health_monitor(
        self, backend_health_monitor, health_monitor: models.HealthMonitor
    ):
        health_monitor.backend_id = backend_health_monitor.id
        health_monitor.provisioning_status = (
            backend_health_monitor.provisioning_status or ""
        )
        health_monitor.operating_status = backend_health_monitor.operating_status or ""
        backend_monitor_type = backend_health_monitor.type
        if backend_monitor_type:
            health_monitor.monitor_type = backend_monitor_type
        health_monitor.delay = backend_health_monitor.delay or health_monitor.delay
        health_monitor.timeout = (
            backend_health_monitor.timeout or health_monitor.timeout
        )
        health_monitor.max_retries = (
            backend_health_monitor.max_retries
            if backend_health_monitor.max_retries is not None
            else health_monitor.max_retries
        )
        health_monitor.max_retries_down = (
            backend_health_monitor.max_retries_down
            if backend_health_monitor.max_retries_down is not None
            else health_monitor.max_retries_down
        )
        health_monitor.error_message = ""
        health_monitor.error_traceback = ""
        health_monitor.save(
            update_fields=[
                "backend_id",
                "provisioning_status",
                "operating_status",
                "monitor_type",
                "delay",
                "timeout",
                "max_retries",
                "max_retries_down",
                "error_message",
                "error_traceback",
            ]
        )
