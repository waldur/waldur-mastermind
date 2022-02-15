import logging
import re
from collections import defaultdict
from typing import Dict

from cinderclient import exceptions as cinder_exceptions
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import create_batch_fetcher, pwgen
from waldur_core.structure.backend import log_backend_action
from waldur_core.structure.registry import get_resource_type
from waldur_core.structure.utils import (
    handle_resource_not_found,
    handle_resource_update_success,
    update_pulled_fields,
)
from waldur_openstack.openstack_base.backend import (
    BaseOpenStackBackend,
    OpenStackBackendError,
)

from . import models
from .log import event_logger

logger = logging.getLogger(__name__)

VALID_ROUTER_INTERFACE_OWNERS = (
    'network:router_interface',
    'network:router_interface_distributed',
    'network:ha_router_replicated_interface',
)


class OpenStackBackend(BaseOpenStackBackend):
    DEFAULTS = {
        'tenant_name': 'admin',
        'verify_ssl': False,
    }

    def validate_settings(self):
        if not self.check_admin_tenant():
            raise ValidationError(_('Provided credentials are not for admin tenant.'))

    def check_admin_tenant(self):
        try:
            self.keystone_admin_client
        except keystone_exceptions.AuthorizationFailure:
            return False
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return True

    def pull_service_properties(self):
        self.pull_flavors()
        self.pull_images()
        self.pull_volume_types()
        self.pull_service_settings_quotas()

    def pull_resources(self):
        self.pull_tenants()

    def pull_subresources(self):
        self.pull_security_groups()
        self.pull_server_groups()
        self.pull_floating_ips()
        self.pull_networks()
        self.pull_subnets()
        self.pull_routers()
        self.pull_ports()

    def pull_tenants(self):
        keystone = self.keystone_admin_client

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
        keystone = self.keystone_admin_client
        return keystone.domains.find(name=self.settings.domain or 'Default')

    def remove_ssh_key_from_tenant(self, tenant, key_name, fingerprint):
        nova = self.nova_client

        # There could be leftovers of key duplicates: remove them all
        keys = nova.keypairs.findall(fingerprint=fingerprint)
        for key in keys:
            # Remove only keys created with Waldur
            if key.name == key_name:
                nova.keypairs.delete(key)

        logger.info('Deleted ssh public key %s from backend', key_name)

    def _are_rules_equal(self, backend_rule, nc_rule):
        if backend_rule['ethertype'] != nc_rule.ethertype:
            return False
        if backend_rule['direction'] != nc_rule.direction:
            return False
        if backend_rule['port_range_min'] != nc_rule.from_port:
            return False
        if backend_rule['port_range_max'] != nc_rule.to_port:
            return False
        if backend_rule['protocol'] != nc_rule.protocol:
            return False
        if backend_rule['remote_ip_prefix'] != nc_rule.cidr:
            return False
        if backend_rule['remote_group_id'] != (
            nc_rule.remote_group.backend_id if nc_rule.remote_group else None
        ):
            return False
        if backend_rule['description'] != nc_rule.description:
            return False
        return True

    def pull_flavors(self):
        nova = self.nova_admin_client
        try:
            flavors = nova.flavors.findall(is_public=True)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        flavor_exclude_regex = self.settings.options.get('flavor_exclude_regex', '')
        name_pattern = (
            re.compile(flavor_exclude_regex) if flavor_exclude_regex else None
        )
        with transaction.atomic():
            cur_flavors = self._get_current_properties(models.Flavor)
            for backend_flavor in flavors:
                if (
                    name_pattern is not None
                    and name_pattern.match(backend_flavor.name) is not None
                ):
                    logger.debug(
                        'Skipping pull of %s flavor as it matches %s regex pattern.',
                        backend_flavor.name,
                        flavor_exclude_regex,
                    )
                    continue

                cur_flavors.pop(backend_flavor.id, None)
                models.Flavor.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_flavor.id,
                    defaults={
                        'name': backend_flavor.name,
                        'cores': backend_flavor.vcpus,
                        'ram': backend_flavor.ram,
                        'disk': self.gb2mb(backend_flavor.disk),
                    },
                )

            models.Flavor.objects.filter(backend_id__in=cur_flavors.keys()).delete()

    def pull_images(self):
        self._pull_images(
            models.Image, lambda image: image['visibility'] == 'public', admin=True
        )

    def _get_current_volume_types(self):
        return self._get_current_properties(models.VolumeType)

    def pull_volume_types(self):
        try:
            volume_types = self.cinder_admin_client.volume_types.list(is_public=True)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            cur_volume_types = self._get_current_volume_types()
            for backend_type in volume_types:
                cur_volume_types.pop(backend_type.id, None)
                models.VolumeType.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_type.id,
                    defaults={
                        'name': backend_type.name,
                        'description': backend_type.description or '',
                    },
                )

            models.VolumeType.objects.filter(
                backend_id__in=cur_volume_types.keys(), settings=self.settings
            ).delete()

    @log_backend_action('push quotas for tenant')
    def push_tenant_quotas(self, tenant, quotas: Dict[str, int]):
        cinder_quotas = {
            'gigabytes': self.mb2gb(quotas.get('storage'))
            if 'storage' in quotas
            else None,
            'volumes': quotas.get('volumes'),
            'snapshots': quotas.get('snapshots'),
        }

        cinder_quotas = {k: v for k, v in cinder_quotas.items() if v is not None}

        # Filter volume-type quotas.
        volume_type_quotas = dict(
            (key, value)
            for (key, value) in quotas.items()
            if key.startswith('gigabytes_') and value is not None
        )

        if volume_type_quotas:
            cinder_quotas.update(volume_type_quotas)

        nova_quotas = {
            'instances': quotas.get('instances'),
            'cores': quotas.get('vcpu'),
            'ram': quotas.get('ram'),
        }
        nova_quotas = {k: v for k, v in nova_quotas.items() if v is not None}

        neutron_quotas = {
            'security_group': quotas.get('security_group_count'),
            'security_group_rule': quotas.get('security_group_rule_count'),
        }
        neutron_quotas = {k: v for k, v in neutron_quotas.items() if v is not None}

        try:
            if cinder_quotas:
                self.cinder_client.quotas.update(tenant.backend_id, **cinder_quotas)
            if nova_quotas:
                self.nova_client.quotas.update(tenant.backend_id, **nova_quotas)
            if neutron_quotas:
                self.neutron_client.update_quota(
                    tenant.backend_id, {'quota': neutron_quotas}
                )
        except Exception as e:
            raise OpenStackBackendError(e)

    @log_backend_action('pull quotas for tenant')
    def pull_tenant_quotas(self, tenant):
        self._pull_tenant_quotas(tenant.backend_id, tenant)

    def pull_quotas(self):
        for tenant in models.Tenant.objects.filter(
            state=models.Tenant.States.OK,
            service_settings=self.settings,
        ):
            self.pull_tenant_quotas(tenant)

    def pull_floating_ips(self, tenants=None):
        if tenants is None:
            tenants = models.Tenant.objects.filter(
                state=models.Tenant.States.OK,
                service_settings=self.settings,
            ).prefetch_related('floating_ips')
        tenant_mappings = {tenant.backend_id: tenant for tenant in tenants}
        if not tenant_mappings:
            return

        backend_floating_ips = self.list_floatingips(list(tenant_mappings.keys()))

        tenant_floating_ips = defaultdict(list)
        for floating_ip in backend_floating_ips:
            tenant_id = floating_ip['tenant_id']
            tenant = tenant_mappings[tenant_id]
            tenant_floating_ips[tenant].append(floating_ip)

        with transaction.atomic():
            for tenant, floating_ips in tenant_floating_ips.items():
                self._update_tenant_floating_ips(tenant, floating_ips)

            self._remove_stale_floating_ips(tenants, backend_floating_ips)

    @method_decorator(create_batch_fetcher)
    def list_floatingips(self, tenants):
        neutron = self.neutron_admin_client

        try:
            return neutron.list_floatingips(tenant_id=tenants)['floatingips']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action('pull floating IPs for tenant')
    def pull_tenant_floating_ips(self, tenant):
        neutron = self.neutron_client

        try:
            backend_floating_ips = neutron.list_floatingips(tenant_id=self.tenant_id)[
                'floatingips'
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            self._update_tenant_floating_ips(tenant, backend_floating_ips)
            self._remove_stale_floating_ips([tenant], backend_floating_ips)

    def _remove_stale_floating_ips(self, tenants, backend_floating_ips):
        remote_ids = {ip['id'] for ip in backend_floating_ips}
        stale_ips = models.FloatingIP.objects.filter(
            tenant__in=tenants,
            state__in=[models.FloatingIP.States.OK, models.FloatingIP.States.ERRED],
        ).exclude(backend_id__in=remote_ids)
        stale_ips.delete()

    def _update_tenant_floating_ips(self, tenant, backend_floating_ips):
        floating_ips = {
            ip.backend_id: ip
            for ip in tenant.floating_ips.filter(
                state__in=[models.FloatingIP.States.OK, models.FloatingIP.States.ERRED]
            )
        }

        for backend_ip in backend_floating_ips:
            imported_floating_ip = self._backend_floating_ip_to_floating_ip(
                backend_ip,
                tenant=tenant,
                service_settings=tenant.service_settings,
                project=tenant.project,
            )
            floating_ip = floating_ips.pop(imported_floating_ip.backend_id, None)
            if floating_ip is None:
                imported_floating_ip.save()
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

    def _backend_floating_ip_to_floating_ip(self, backend_floating_ip, **kwargs):
        port_id = backend_floating_ip['port_id']
        if port_id:
            port = models.Port.objects.filter(
                backend_id=port_id,
                service_settings=self.settings,
            ).first()
        else:
            port = None
        floating_ip = models.FloatingIP(
            name=backend_floating_ip['floating_ip_address'],
            description=backend_floating_ip['description'],
            address=backend_floating_ip['floating_ip_address'],
            backend_network_id=backend_floating_ip['floating_network_id'],
            runtime_state=backend_floating_ip['status'],
            backend_id=backend_floating_ip['id'],
            state=models.FloatingIP.States.OK,
            port=port,
        )
        for field, value in kwargs.items():
            setattr(floating_ip, field, value)

        return floating_ip

    def pull_security_groups(self, tenants=None):

        if tenants is None:
            tenants = models.Tenant.objects.filter(
                state=models.Tenant.States.OK,
                service_settings=self.settings,
            ).prefetch_related('security_groups')
        tenant_mappings = {tenant.backend_id: tenant for tenant in tenants}
        if not tenant_mappings:
            return

        backend_security_groups = self.list_security_groups(
            list(tenant_mappings.keys())
        )

        tenant_security_groups = defaultdict(list)
        for security_group in backend_security_groups:
            tenant_id = security_group['tenant_id']
            tenant = tenant_mappings[tenant_id]
            tenant_security_groups[tenant].append(security_group)

        with transaction.atomic():
            for tenant, security_groups in tenant_security_groups.items():
                self._update_tenant_security_groups(tenant, security_groups)
            self._remove_stale_security_groups(tenants, backend_security_groups)

    @method_decorator(create_batch_fetcher)
    def list_security_groups(self, tenants):
        neutron = self.neutron_admin_client

        try:
            return neutron.list_security_groups(tenant_id=tenants)['security_groups']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    def pull_security_group(self, local_security_group: models.SecurityGroup):
        neutron = self.neutron_client
        try:
            remote_security_group = neutron.show_security_group(
                local_security_group.backend_id
            )['security_group']
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

    @log_backend_action('pull security groups for tenant')
    def pull_tenant_security_groups(self, tenant):
        neutron = self.neutron_client
        try:
            backend_security_groups = neutron.list_security_groups(
                tenant_id=self.tenant_id
            )['security_groups']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            self._update_tenant_security_groups(tenant, backend_security_groups)
            self._remove_stale_security_groups([tenant], backend_security_groups)

    def _remove_stale_security_groups(self, tenants, backend_security_groups):
        remote_ids = {ip['id'] for ip in backend_security_groups}
        stale_groups = models.SecurityGroup.objects.filter(
            tenant__in=tenants,
            state__in=[
                models.SecurityGroup.States.OK,
                models.SecurityGroup.States.ERRED,
            ],
        ).exclude(backend_id__in=remote_ids)
        for security_group in stale_groups:
            event_logger.openstack_security_group.info(
                'Security group %s has been cleaned from cache.' % security_group.name,
                event_type='openstack_security_group_cleaned',
                event_context={
                    'security_group': security_group,
                },
            )
        stale_groups.delete()

    def _update_tenant_security_groups(self, tenant, backend_security_groups):
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
                        'Skipping pulling of OpenStack security group because it is '
                        'not in the stable state. Group ID: %s',
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

    def _log_security_group_imported(self, security_group):
        event_logger.openstack_security_group.info(
            'Security group %s has been imported to local cache.' % security_group.name,
            event_type='openstack_security_group_imported',
            event_context={'security_group': security_group},
        )

    def _log_security_group_pulled(self, security_group):
        event_logger.openstack_security_group.info(
            'Security group %s has been pulled from backend.' % security_group.name,
            event_type='openstack_security_group_pulled',
            event_context={'security_group': security_group},
        )

    def _log_security_group_rule_imported(self, rule):
        event_logger.openstack_security_group_rule.info(
            'Security group rule %s has been imported from backend.' % str(rule),
            event_type='openstack_security_group_rule_imported',
            event_context={'security_group_rule': rule},
        )

    def _log_security_group_rule_pulled(self, rule):
        logger.debug('Security group rule %s has been pulled from backend.', str(rule))

    def _log_security_group_rule_cleaned(self, rule):
        event_logger.openstack_security_group_rule.info(
            'Security group rule %s has been cleaned from cache.' % str(rule),
            event_type='openstack_security_group_rule_cleaned',
            event_context={'security_group_rule': rule},
        )

    def _update_remote_security_groups(self, tenant, backend_security_groups):
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
            for backend_rule in backend_security_group['security_group_rules']:
                security_group_rule = security_group_rule_map.get(backend_rule['id'])
                remote_group = security_group_map.get(backend_rule['remote_group_id'])
                if not security_group_rule:
                    continue
                if security_group_rule.remote_group != remote_group:
                    security_group_rule.remote_group = remote_group
                    security_group_rule.save(update_fields=['remote_group'])

    def _backend_security_group_to_security_group(
        self, backend_security_group, **kwargs
    ):
        security_group = models.SecurityGroup(
            name=backend_security_group['name'],
            description=backend_security_group['description'],
            backend_id=backend_security_group['id'],
            state=models.SecurityGroup.States.OK,
        )

        for field, value in kwargs.items():
            setattr(security_group, field, value)

        return security_group

    def pull_routers(self):
        for tenant in models.Tenant.objects.filter(
            state=models.Tenant.States.OK,
            service_settings=self.settings,
        ):
            self.pull_tenant_routers(tenant)

    def pull_tenant_routers(self, tenant):
        neutron = self.neutron_admin_client

        try:
            backend_routers = neutron.list_routers(tenant_id=tenant.backend_id)[
                'routers'
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for backend_router in backend_routers:
            backend_id = backend_router['id']
            try:
                ports = neutron.list_ports(device_id=backend_id)['ports']
                fixed_ips = []
                for port in ports:
                    for fixed_ip in port['fixed_ips']:
                        fixed_ips.append(fixed_ip['ip_address'])
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

            defaults = {
                'name': backend_router['name'],
                'description': backend_router['description'],
                'routes': backend_router['routes'],
                'fixed_ips': fixed_ips,
                'service_settings': tenant.service_settings,
                'project': tenant.project,
                'state': models.Router.States.OK,
            }
            try:
                models.Router.objects.update_or_create(
                    tenant=tenant, backend_id=backend_id, defaults=defaults
                )
            except IntegrityError:
                logger.warning(
                    'Could not create router with backend ID %s '
                    'and tenant %s due to concurrent update.',
                    backend_id,
                    tenant,
                )

        remote_ids = {ip['id'] for ip in backend_routers}
        stale_routers = models.Router.objects.filter(tenant=tenant).exclude(
            backend_id__in=remote_ids
        )
        stale_routers.delete()

    def pull_ports(self):
        for tenant in models.Tenant.objects.filter(
            state=models.Tenant.States.OK,
            service_settings=self.settings,
        ):
            self.pull_tenant_ports(tenant)

    def pull_tenant_ports(self, tenant):
        neutron = self.neutron_admin_client

        try:
            backend_ports = neutron.list_ports(tenant_id=tenant.backend_id)['ports']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        networks = models.Network.objects.filter(tenant=tenant)
        network_mappings = {network.backend_id: network for network in networks}

        security_groups = models.SecurityGroup.objects.filter(tenant=tenant)
        security_group_mappings = {
            security_group.backend_id: security_group
            for security_group in security_groups
        }

        for backend_port in backend_ports:
            backend_id = backend_port['id']
            defaults = {
                'name': backend_port['name'],
                'description': backend_port['description'],
                'service_settings': tenant.service_settings,
                'project': tenant.project,
                'state': models.Port.States.OK,
                'mac_address': backend_port['mac_address'],
                'fixed_ips': backend_port['fixed_ips'],
                'allowed_address_pairs': backend_port.get('allowed_address_pairs', []),
                'network': network_mappings.get(backend_port['network_id']),
                'device_id': backend_port.get('device_id'),
                'device_owner': backend_port.get('device_owner'),
                'port_security_enabled': backend_port.get(
                    'port_security_enabled', True
                ),
            }
            try:
                port, _ = models.Port.objects.update_or_create(
                    tenant=tenant, backend_id=backend_id, defaults=defaults
                )
                local_groups = set(
                    port.security_groups.values_list('backend_id', flat=True)
                )
                remote_groups = set(backend_port['security_groups'])

                new_groups = remote_groups - local_groups
                for group_id in new_groups:
                    security_groups = security_group_mappings.get(group_id)
                    if security_groups:
                        port.security_groups.add(security_groups)

                stale_groups = local_groups - remote_groups
                for group in port.security_groups.filter(backend_id__in=stale_groups):
                    port.security_groups.remove(group)
            except IntegrityError:
                logger.warning(
                    'Could not create or update port with backend ID %s '
                    'and tenant %s due to concurrent update.',
                    backend_id,
                    tenant,
                )

        remote_ids = {ip['id'] for ip in backend_ports}
        stale_ports = models.Port.objects.filter(tenant=tenant).exclude(
            backend_id__in=remote_ids
        )
        stale_ports.delete()

    def pull_networks(self):
        tenants = (
            models.Tenant.objects.exclude(backend_id='')
            .filter(
                state__in=[models.Tenant.States.OK, models.Tenant.States.UPDATING],
                service_settings=self.settings,
            )
            .prefetch_related('networks')
        )

        self._pull_networks(tenants)

    def pull_tenant_networks(self, tenant):
        self._pull_networks([tenant])

    def _pull_networks(self, tenants):
        tenant_mappings = {tenant.backend_id: tenant for tenant in tenants}
        backend_networks = self.list_networks(list(tenant_mappings.keys()))

        networks = []
        with transaction.atomic():
            for backend_network in backend_networks:
                tenant = tenant_mappings.get(backend_network['tenant_id'])
                if not tenant:
                    logger.debug(
                        'Skipping network %s synchronization because its tenant %s is not available.',
                        backend_network['id'],
                        backend_network['tenant_id'],
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
                        'Network %s has been imported to local cache.' % network.name,
                        event_type='openstack_network_imported',
                        event_context={
                            'network': network,
                        },
                    )
                else:
                    modified = update_pulled_fields(
                        network, imported_network, models.Network.get_backend_fields()
                    )
                    handle_resource_update_success(network)
                    if modified:
                        event_logger.openstack_network.info(
                            'Network %s has been pulled from backend.' % network.name,
                            event_type='openstack_network_pulled',
                            event_context={
                                'network': network,
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
                    'Network %s has been cleaned from cache.' % network.name,
                    event_type='openstack_network_cleaned',
                    event_context={
                        'network': network,
                    },
                )
            stale_networks.delete()

        return networks

    @method_decorator(create_batch_fetcher)
    def list_networks(self, tenants):
        neutron = self.neutron_admin_client
        try:
            return neutron.list_networks(tenant_id=tenants)['networks']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    def _backend_network_to_network(self, backend_network, **kwargs):
        network = models.Network(
            name=backend_network['name'],
            description=backend_network['description'],
            is_external=backend_network['router:external'],
            runtime_state=backend_network['status'],
            mtu=backend_network.get('mtu'),
            backend_id=backend_network['id'],
            state=models.Network.States.OK,
        )
        if backend_network.get('provider:network_type'):
            network.type = backend_network['provider:network_type']
        if backend_network.get('provider:segmentation_id'):
            network.segmentation_id = backend_network['provider:segmentation_id']

        for field, value in kwargs.items():
            setattr(network, field, value)

        return network

    def pull_subnets(self, tenant=None, network=None):
        neutron = self.neutron_admin_client

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
                    'subnets'
                ]
            elif network:
                backend_subnets = neutron.list_subnets(network_id=network.backend_id)[
                    'subnets'
                ]
            else:
                # We can't filter subnets by network IDs because it exceeds maximum request length
                backend_subnets = neutron.list_subnets()['subnets']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        subnet_uuids = []
        with transaction.atomic():
            for backend_subnet in backend_subnets:
                network = network_mappings.get(backend_subnet['network_id'])

                if not network:
                    logger.debug(
                        'OpenStack network is not imported yet. Network ID: %s',
                        backend_subnet['network_id'],
                    )
                    continue

                imported_subnet = self._backend_subnet_to_subnet(
                    backend_subnet,
                    network=network,
                    service_settings=network.service_settings,
                    project=network.project,
                )

                try:
                    subnet = models.SubNet.objects.get(
                        network=network, backend_id=imported_subnet.backend_id
                    )
                except models.SubNet.DoesNotExist:
                    imported_subnet.save()
                    subnet = imported_subnet

                    event_logger.openstack_subnet.info(
                        'SubNet %s has been imported to local cache.' % subnet.name,
                        event_type='openstack_subnet_imported',
                        event_context={
                            'subnet': subnet,
                        },
                    )

                else:
                    modified = update_pulled_fields(
                        subnet, imported_subnet, models.SubNet.get_backend_fields()
                    )
                    handle_resource_update_success(subnet)
                    if modified:
                        event_logger.openstack_subnet.info(
                            'SubNet %s has been pulled from backend.' % subnet.name,
                            event_type='openstack_subnet_pulled',
                            event_context={
                                'subnet': subnet,
                            },
                        )

                subnet_uuids.append(subnet.uuid)

            stale_subnets = models.SubNet.objects.filter(
                state__in=[models.SubNet.States.OK, models.SubNet.States.ERRED],
                network__in=networks,
            ).exclude(uuid__in=subnet_uuids)
            for subnet in stale_subnets:
                event_logger.openstack_subnet.info(
                    'SubNet %s has been cleaned.' % subnet.name,
                    event_type='openstack_subnet_cleaned',
                    event_context={
                        'subnet': subnet,
                    },
                )
            stale_subnets.delete()

    @log_backend_action()
    def import_tenant_subnets(self, tenant):
        self.pull_subnets(tenant)

    def _backend_subnet_to_subnet(self, backend_subnet, **kwargs):
        subnet = models.SubNet(
            name=backend_subnet['name'],
            description=backend_subnet['description'],
            allocation_pools=backend_subnet['allocation_pools'],
            cidr=backend_subnet['cidr'],
            ip_version=backend_subnet['ip_version'],
            enable_dhcp=backend_subnet['enable_dhcp'],
            gateway_ip=backend_subnet.get('gateway_ip'),
            dns_nameservers=backend_subnet['dns_nameservers'],
            host_routes=sorted(
                backend_subnet.get('host_routes', []), key=lambda x: tuple(x.values())
            ),
            backend_id=backend_subnet['id'],
            state=models.SubNet.States.OK,
        )

        for field, value in kwargs.items():
            setattr(subnet, field, value)

        return subnet

    @log_backend_action()
    def create_tenant(self, tenant):
        keystone = self.keystone_admin_client
        try:
            backend_tenant = keystone.projects.create(
                name=tenant.name,
                description=tenant.description,
                domain=self._get_domain(),
            )
            tenant.backend_id = backend_tenant.id
            tenant.save(update_fields=['backend_id'])
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_tenant_safe(self, tenant):
        """
        Check available tenant name before creating tenant.
        It allows to avoid failure when name is already taken.
        """
        new_name = self.get_available_tenant_name(tenant.name)
        if new_name != tenant.name:
            tenant.name = new_name
            tenant.save(update_fields=['name'])
        self.create_tenant(tenant)

    def get_available_tenant_name(self, name, max_length=64):
        """
        Returns a tenant name that's free on the target deployment.
        """
        keystone = self.keystone_admin_client
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
            new_name = "%s_%s" % (name, get_random_string(3))
            truncation = len(new_name) - max_length
            if truncation > 0:
                new_name = "%s_%s" % (name[:-truncation], get_random_string(3))
        return new_name

    def _import_tenant(
        self, tenant_backend_id, service_settings=None, project=None, save=True
    ):
        keystone = self.keystone_admin_client
        try:
            backend_tenant = keystone.projects.get(tenant_backend_id)
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
        keystone = self.keystone_admin_client
        try:
            tenants = [
                {
                    'type': get_resource_type(models.Tenant),
                    'name': tenant.name,
                    'description': tenant.description,
                    'backend_id': tenant.id,
                }
                for tenant in keystone.projects.list(domain=self._get_domain())
            ]
            return self.get_importable_resources(models.Tenant, tenants)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def pull_tenant(self, tenant):
        import_time = timezone.now()
        imported_tenant = self._import_tenant(tenant.backend_id, save=False)

        tenant.refresh_from_db()
        # if tenant was not modified in Waldur database after import.
        if tenant.modified < import_time:
            update_pulled_fields(tenant, imported_tenant, ('name', 'description'))

    @log_backend_action()
    def add_admin_user_to_tenant(self, tenant):
        """Add user from openstack settings to new tenant"""
        keystone = self.keystone_admin_client

        try:
            admin_user = keystone.users.find(name=self.settings.username)
            admin_role = keystone.roles.find(name='admin')
            try:
                keystone.roles.grant(
                    user=admin_user.id, role=admin_role.id, project=tenant.backend_id
                )
            except keystone_exceptions.Conflict:
                pass
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action('add user to tenant')
    def create_tenant_user(self, tenant):
        keystone = self.keystone_client

        try:
            user = keystone.users.create(
                name=tenant.user_username,
                password=tenant.user_password,
                domain=self._get_domain(),
            )
            try:
                role = keystone.roles.find(name='Member')
            except keystone_exceptions.NotFound:
                role = keystone.roles.find(name='_member_')
            keystone.roles.grant(
                user=user.id,
                role=role.id,
                project=tenant.backend_id,
            )
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def create_or_update_tenant_user(self, tenant):
        keystone = self.keystone_client

        try:
            keystone_user = keystone.users.find(name=tenant.user_username)
        except keystone_exceptions.NotFound:
            self.create_tenant_user(tenant)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            self.change_tenant_user_password(tenant, keystone_user)

    @log_backend_action('change password for tenant user')
    def change_tenant_user_password(self, tenant, keystone_user=None):
        keystone = self.keystone_client

        try:
            if not keystone_user:
                keystone_user = keystone.users.find(name=tenant.user_username)
            keystone.users.update(user=keystone_user, password=tenant.user_password)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_floating_ips(self, tenant):
        if not tenant.backend_id:
            # This method will remove all floating IPs if tenant `backend_id` is not defined.
            raise OpenStackBackendError(
                'This method should not be called if tenant has no backend_id'
            )

        neutron = self.neutron_admin_client

        try:
            floatingips = neutron.list_floatingips(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for floating_ip in floatingips.get('floatingips', []):
            self._delete_backend_floating_ip(floating_ip['id'], tenant.backend_id)

    @log_backend_action()
    def delete_tenant_ports(self, tenant):
        if not tenant.backend_id:
            # This method will remove all ports if tenant `backend_id` is not defined.
            raise OpenStackBackendError(
                'This method should not be called if tenant has no backend_id'
            )

        neutron = self.neutron_admin_client

        try:
            ports = neutron.list_ports(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for port in ports.get('ports', []):
            if (
                'device_id' in port
                and port['device_owner'] in VALID_ROUTER_INTERFACE_OWNERS
            ):
                logger.info(
                    "Deleting port %s interface_router from tenant %s",
                    port['id'],
                    tenant.backend_id,
                )
                try:
                    neutron.remove_interface_router(
                        port['device_id'], {'port_id': port['id']}
                    )
                except neutron_exceptions.NotFound:
                    logger.debug(
                        "Port %s interface_router is already gone from tenant %s",
                        port['id'],
                        tenant.backend_id,
                    )
                except neutron_exceptions.NeutronClientException as e:
                    raise OpenStackBackendError(e)

            logger.info(
                "Deleting port %s from tenant %s", port['id'], tenant.backend_id
            )
            try:
                neutron.delete_port(port['id'])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Port %s is already gone from tenant %s",
                    port['id'],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_routes(self, tenant):
        if not tenant.backend_id:
            # This method will remove all routers if tenant `backend_id` is not defined.
            raise OpenStackBackendError(
                'This method should not be called if tenant has no backend_id'
            )

        neutron = self.neutron_admin_client

        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for router in routers.get('routers', []):
            if not router['routes']:
                continue
            logger.info(
                "Deleting routes for router %s from tenant %s",
                router['id'],
                tenant.backend_id,
            )
            try:
                neutron.update_router(router['id'], {'router': {'routes': []}})
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_routers(self, tenant):
        if not tenant.backend_id:
            # This method will remove all routers if tenant `backend_id` is not defined.
            raise OpenStackBackendError(
                'This method should not be called if tenant has no backend_id'
            )

        neutron = self.neutron_admin_client

        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for router in routers.get('routers', []):
            logger.info(
                "Deleting router %s from tenant %s", router['id'], tenant.backend_id
            )
            try:
                neutron.delete_router(router['id'])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Router %s is already gone from tenant %s",
                    router['id'],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_networks(self, tenant):
        if not tenant.backend_id:
            # This method will remove all networks if tenant `backend_id` is not defined.
            raise OpenStackBackendError(
                'This method should not be called if tenant has no backend_id'
            )

        neutron = self.neutron_admin_client

        try:
            networks = neutron.list_networks(tenant_id=tenant.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for network in networks.get('networks', []):
            if network['router:external']:
                continue
            for subnet in network['subnets']:
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
                "Deleting network %s from tenant %s", network['id'], tenant.backend_id
            )
            try:
                neutron.delete_network(network['id'])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Network %s is already gone from tenant %s",
                    network['id'],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

        tenant.set_quota_usage(tenant.Quotas.network_count, 0)
        tenant.set_quota_usage(tenant.Quotas.subnet_count, 0)

    @log_backend_action()
    def delete_tenant_security_groups(self, tenant):
        neutron = self.neutron_client

        try:
            sgroups = neutron.list_security_groups(tenant_id=tenant.backend_id)[
                'security_groups'
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for sgroup in sgroups:
            logger.info(
                "Deleting security group %s from tenant %s",
                sgroup['id'],
                tenant.backend_id,
            )
            try:
                neutron.delete_security_group(sgroup['id'])
            except neutron_exceptions.NotFound:
                logger.debug(
                    "Security group %s is already gone from tenant %s",
                    sgroup['id'],
                    tenant.backend_id,
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_tenant_instances(self, tenant):
        nova = self.nova_client

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

    def are_all_tenant_instances_deleted(self, tenant):
        nova = self.nova_client

        try:
            servers = nova.servers.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return not servers

    @log_backend_action()
    def delete_tenant_snapshots(self, tenant):
        cinder = self.cinder_client

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
    def are_all_tenant_snapshots_deleted(self, tenant):
        cinder = self.cinder_client

        try:
            snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return not snapshots

    @log_backend_action()
    def delete_tenant_volumes(self, tenant):
        cinder = self.cinder_client

        try:
            volumes = cinder.volumes.list()
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
    def are_all_tenant_volumes_deleted(self, tenant):
        cinder = self.cinder_client

        try:
            volumes = cinder.volumes.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        else:
            return not volumes

    @log_backend_action()
    def delete_tenant_user(self, tenant):
        keystone = self.keystone_client
        try:
            user = keystone.users.find(name=tenant.user_username)
            logger.info(
                'Deleting user %s that was connected to tenant %s',
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
                'Cannot delete user %s from tenant %s. Error: %s',
                tenant.user_username,
                tenant.backend_id,
                e,
            )

    @log_backend_action()
    def delete_tenant(self, tenant):
        if not tenant.backend_id:
            raise OpenStackBackendError(
                'This method should not be called if tenant has no backend_id'
            )

        keystone = self.keystone_admin_client

        logger.info("Deleting tenant %s", tenant.backend_id)
        try:
            keystone.projects.delete(tenant.backend_id)
        except keystone_exceptions.NotFound:
            logger.debug("Tenant %s is already gone", tenant.backend_id)
        except keystone_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def push_security_group_rules(self, security_group):
        neutron = self.neutron_client

        try:
            backend_security_group = neutron.show_security_group(
                security_group.backend_id
            )['security_group']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        backend_rules = {
            rule['id']: self._normalize_security_group_rule(rule)
            for rule in backend_security_group['security_group_rules']
        }

        # list of waldur rules, that do not exist in openstack
        nonexistent_rules = []
        # list of waldur rules, that have wrong parameters in openstack
        unsynchronized_rules = []
        # list of os rule ids, that exist in openstack and do not exist in waldur
        extra_rule_ids = list(backend_rules.keys())

        for nc_rule in security_group.rules.all():
            if nc_rule.backend_id not in backend_rules:
                nonexistent_rules.append(nc_rule)
            else:
                backend_rule = backend_rules[nc_rule.backend_id]
                if not self._are_rules_equal(backend_rule, nc_rule):
                    unsynchronized_rules.append(nc_rule)
                extra_rule_ids.remove(nc_rule.backend_id)

        # deleting extra rules
        for backend_rule_id in extra_rule_ids:
            logger.debug(
                'About to delete security group rule with id %s in backend',
                backend_rule_id,
            )
            try:
                neutron.delete_security_group_rule(backend_rule_id)
            except neutron_exceptions.NeutronClientException:
                logger.exception(
                    'Failed to remove rule with id %s from security group %s in backend',
                    backend_rule_id,
                    security_group,
                )
            else:
                logger.info(
                    'Security group rule with id %s successfully deleted in backend',
                    backend_rule_id,
                )
                backend_rule = backend_rules[backend_rule_id]
                security_group_rule = models.SecurityGroupRule(
                    security_group=security_group,
                    backend_id=backend_rule_id,
                    **self._import_security_group_rule(backend_rule),
                )
                event_logger.openstack_security_group_rule.info(
                    'Extra security group rule %s has been deleted in '
                    'backend because it is not defined in Waldur.'
                    % str(security_group_rule),
                    event_type='openstack_security_group_rule_deleted',
                    event_context={'security_group_rule': security_group_rule},
                )

        # deleting unsynchronized rules
        for nc_rule in unsynchronized_rules:
            logger.debug(
                'About to delete security group rule with id %s', nc_rule.backend_id
            )
            try:
                neutron.delete_security_group_rule(nc_rule.backend_id)
            except neutron_exceptions.NeutronClientException:
                logger.exception(
                    'Failed to remove rule with id %s from security group %s in backend',
                    nc_rule.backend_id,
                    security_group,
                )
            else:
                logger.info(
                    'Security group rule with id %s successfully deleted in backend',
                    nc_rule.backend_id,
                )
                event_logger.openstack_security_group_rule.info(
                    'Security group rule %s has been deleted '
                    'from backend because it has different params.' % str(nc_rule),
                    event_type='openstack_security_group_rule_deleted',
                    event_context={'security_group_rule': nc_rule},
                )

        # creating nonexistent and unsynchronized rules
        for nc_rule in unsynchronized_rules + nonexistent_rules:
            logger.debug(
                'About to create security group rule with id %s in backend', nc_rule.id
            )
            try:
                # The database has empty strings instead of nulls
                if nc_rule.protocol == '':
                    nc_rule_protocol = None
                else:
                    nc_rule_protocol = nc_rule.protocol

                sec_group_rule = neutron.create_security_group_rule(
                    {
                        'security_group_rule': {
                            'security_group_id': security_group.backend_id,
                            'ethertype': nc_rule.ethertype,
                            'direction': nc_rule.direction,
                            'protocol': nc_rule_protocol,
                            'port_range_min': nc_rule.from_port
                            if nc_rule.from_port != -1
                            else None,
                            'port_range_max': nc_rule.to_port
                            if nc_rule.to_port != -1
                            else None,
                            'remote_ip_prefix': nc_rule.cidr,
                            'remote_group_id': nc_rule.remote_group.backend_id
                            if nc_rule.remote_group
                            else None,
                            'description': nc_rule.description,
                        }
                    }
                )

                new_backend_id = sec_group_rule['security_group_rule']['id']
                if new_backend_id != nc_rule.backend_id:
                    nc_rule.backend_id = new_backend_id
                    nc_rule.save(update_fields=['backend_id'])
            except neutron_exceptions.NeutronClientException as e:
                logger.exception(
                    'Failed to create rule %s for security group %s in backend',
                    nc_rule,
                    security_group,
                )
                raise OpenStackBackendError(e)
            else:
                logger.info(
                    'Security group rule with id %s successfully created in backend',
                    nc_rule.id,
                )
                event_logger.openstack_security_group_rule.info(
                    'Security group rule %s has been created in backend.'
                    % str(nc_rule),
                    event_type='openstack_security_group_rule_created',
                    event_context={'security_group_rule': nc_rule},
                )

    @log_backend_action()
    def create_security_group(self, security_group):
        neutron = self.neutron_client
        try:
            backend_security_group = neutron.create_security_group(
                {
                    'security_group': {
                        'name': security_group.name,
                        'description': security_group.description,
                    }
                }
            )['security_group']
            security_group.backend_id = backend_security_group['id']
            security_group.save(update_fields=['backend_id'])
            self.push_security_group_rules(security_group)

            event_logger.openstack_security_group.info(
                'Security group "%s" has been created in the backend.'
                % security_group.name,
                event_type='openstack_security_group_created',
                event_context={
                    'security_group': security_group,
                },
            )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_security_group(self, security_group):
        neutron = self.neutron_client
        try:
            neutron.delete_security_group(security_group.backend_id)

            event_logger.openstack_security_group.info(
                'Security group "%s" has been deleted' % security_group.name,
                event_type='openstack_security_group_deleted',
                event_context={
                    'security_group': security_group,
                },
            )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        security_group.decrease_backend_quotas_usage()
        security_group.delete()

    def detach_security_group_from_all_instances(self, security_group):
        connected_instances = self.get_instances_connected_to_security_groups(
            security_group
        )
        for instance_id in connected_instances:
            self.detach_security_group_from_instance(
                security_group.backend_id, instance_id
            )

    def get_instances_connected_to_security_groups(self, security_group):
        nova = self.nova_client
        try:
            instances = nova.servers.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        connected_instances = set()
        for instance in instances:
            if hasattr(
                instance, 'security_groups'
            ):  # can be missing if instance is being deleted
                for group in instance.security_groups:
                    if security_group.name == group['name']:
                        connected_instances.add(instance.id)
        return connected_instances

    def detach_security_group_from_instance(self, group_id, server_id):
        nova = self.nova_client
        try:
            nova.servers.remove_security_group(server_id, group_id)
        except nova_exceptions.ClientException:
            logger.exception(
                'Failed to remove security group %s from instance %s',
                group_id,
                server_id,
            )
        else:
            logger.info(
                'Removed security group %s from instance %s', group_id, server_id
            )

    def detach_security_group_from_all_ports(self, security_group):
        neutron = self.neutron_admin_client
        try:
            remote_ports = neutron.list_ports(
                tenant_id=security_group.tenant.backend_id
            )['ports']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for remote_port in remote_ports:
            # Neutron REST API doesn't allow to filter ports by security groups
            if security_group.backend_id not in remote_port['security_groups']:
                continue
            security_groups = remote_port['security_groups']
            security_groups.remove(security_group.backend_id)
            try:
                neutron.update_port(
                    remote_port['id'],
                    {'port': {'security_groups': security_groups}},
                )
            except neutron_exceptions.NeutronClientException as e:
                raise OpenStackBackendError(e)

    @log_backend_action()
    def update_security_group(self, security_group):
        neutron = self.neutron_client
        data = {'name': security_group.name, 'description': security_group.description}
        try:
            neutron.update_security_group(
                security_group.backend_id, {'security_group': data}
            )
            self.push_security_group_rules(security_group)

            event_logger.openstack_security_group.info(
                'Security group "%s" has been updated' % security_group.name,
                event_type='openstack_security_group_updated',
                event_context={
                    'security_group': security_group,
                },
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def set_static_routes(self, router):
        neutron = self.neutron_client
        try:
            neutron.update_router(
                router.backend_id, {'router': {'routes': router.routes}}
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def detect_external_network(self, tenant):
        neutron = self.neutron_client
        try:
            routers = neutron.list_routers(tenant_id=tenant.backend_id)['routers']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        if bool(routers):
            router = routers[0]
        else:
            logger.warning(
                'Tenant %s (PK: %s) does not have connected routers.', tenant, tenant.pk
            )
            return

        ext_gw = router.get('external_gateway_info', {})
        if ext_gw and 'network_id' in ext_gw:
            tenant.external_network_id = ext_gw['network_id']
            tenant.save()
            logger.info(
                'Found and set external network with id %s for tenant %s (PK: %s)',
                ext_gw['network_id'],
                tenant,
                tenant.pk,
            )

    @log_backend_action()
    def create_network(self, network):
        neutron = self.neutron_admin_client

        data = {'name': network.name, 'tenant_id': network.tenant.backend_id}
        try:
            response = neutron.create_network({'networks': [data]})
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            backend_network = response['networks'][0]
            network.backend_id = backend_network['id']
            network.runtime_state = backend_network['status']
            if backend_network.get('provider:network_type'):
                network.type = backend_network['provider:network_type']
            if backend_network.get('provider:segmentation_id'):
                network.segmentation_id = backend_network['provider:segmentation_id']
            network.save()
            # XXX: temporary fix - right now backend logic is based on statement "one tenant has one network"
            # We need to fix this in the future.
            network.tenant.internal_network_id = network.backend_id
            network.tenant.save()

            event_logger.openstack_network.info(
                'Network %s has been created in the backend.' % network.name,
                event_type='openstack_network_created',
                event_context={
                    'network': network,
                },
            )

    def _update_network(self, network_id, data):
        neutron = self.neutron_admin_client

        try:
            neutron.update_network(network_id, {'network': data})
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def update_network(self, network):
        self._update_network(network.backend_id, {'name': network.name})
        event_logger.openstack_network.info(
            'Network name %s has been updated.' % network.name,
            event_type='openstack_network_updated',
            event_context={'network': network},
        )

    @log_backend_action()
    def set_network_mtu(self, network):
        self._update_network(network.backend_id, {'mtu': network.mtu})
        event_logger.openstack_network.info(
            'Network MTU %s has been updated.' % network.name,
            event_type='openstack_network_updated',
            event_context={'network': network},
        )

    @log_backend_action()
    def delete_network(self, network):
        for subnet in network.subnets.all():
            self.delete_subnet(subnet)

        neutron = self.neutron_admin_client
        try:
            neutron.delete_network(network.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            network.decrease_backend_quotas_usage()
            event_logger.openstack_network.info(
                'Network %s has been deleted' % network.name,
                event_type='openstack_network_deleted',
                event_context={
                    'network': network,
                },
            )

    @log_backend_action()
    def import_tenant_networks(self, tenant):
        networks = self._pull_networks([tenant])
        if networks:
            # XXX: temporary fix - right now backend logic is based on statement "one tenant has one network"
            # We need to fix this in the future.
            tenant.internal_network_id = networks[0].backend_id
            tenant.save()

    def import_network(self, network_backend_id):
        neutron = self.neutron_admin_client
        try:
            backend_network = neutron.show_network(network_backend_id)['network']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        return self._backend_network_to_network(backend_network)

    @log_backend_action()
    def pull_network(self, network):
        import_time = timezone.now()
        imported_network = self.import_network(network.backend_id)

        network.refresh_from_db()
        if network.modified < import_time:
            modified = update_pulled_fields(
                network, imported_network, models.Network.get_backend_fields()
            )
            if modified:
                event_logger.openstack_network.info(
                    'Network %s has been pulled from backend.' % network.name,
                    event_type='openstack_network_pulled',
                    event_context={'network': network},
                )

        self.pull_subnets(network=network)

    @log_backend_action()
    def create_subnet(self, subnet):
        neutron = self.neutron_admin_client

        data = {
            'name': subnet.name,
            'network_id': subnet.network.backend_id,
            'tenant_id': subnet.network.tenant.backend_id,
            'cidr': subnet.cidr,
            'allocation_pools': subnet.allocation_pools,
            'ip_version': subnet.ip_version,
            'enable_dhcp': subnet.enable_dhcp,
        }
        if subnet.dns_nameservers:
            data['dns_nameservers'] = subnet.dns_nameservers
        if subnet.host_routes:
            data['host_routes'] = subnet.host_routes
        if subnet.disable_gateway:
            data['gateway_ip'] = None
        elif subnet.gateway_ip:
            data['gateway_ip'] = subnet.gateway_ip
        try:
            response = neutron.create_subnet({'subnets': [data]})
            backend_subnet = response['subnets'][0]
            subnet.backend_id = backend_subnet['id']
            if backend_subnet.get('gateway_ip'):
                subnet.gateway_ip = backend_subnet['gateway_ip']

            # Automatically create router for subnet
            # TODO: Ideally: Create separate model for router and create it separately.
            self.connect_subnet(subnet)
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            subnet.save()

            event_logger.openstack_subnet.info(
                'SubNet %s has been created in the backend.' % subnet.name,
                event_type='openstack_subnet_created',
                event_context={
                    'subnet': subnet,
                },
            )

    @log_backend_action()
    def update_subnet(self, subnet):
        neutron = self.neutron_admin_client

        data = {
            'name': subnet.name,
            'dns_nameservers': subnet.dns_nameservers,
            'host_routes': subnet.host_routes,
        }

        # We should send gateway_ip only when it is changed, because
        # updating gateway_ip is prohibited when the ip is used.
        try:
            backend_subnet = neutron.show_subnet(subnet.backend_id)['subnet']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        if backend_subnet['gateway_ip'] != subnet.gateway_ip:
            data['gateway_ip'] = subnet.gateway_ip

        try:
            neutron.update_subnet(subnet.backend_id, {'subnet': data})
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            event_logger.openstack_subnet.info(
                'SubNet %s has been updated' % subnet.name,
                event_type='openstack_subnet_updated',
                event_context={
                    'subnet': subnet,
                },
            )

    def disconnect_subnet(self, subnet):
        neutron = self.neutron_admin_client
        try:
            ports = neutron.list_ports(network_id=subnet.network.backend_id)['ports']

            for port in ports:
                if port['device_owner'] not in VALID_ROUTER_INTERFACE_OWNERS:
                    continue
                neutron.remove_interface_router(
                    port['device_id'], {'subnet_id': subnet.backend_id}
                )

        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        else:
            subnet.is_connected = False
            subnet.save(update_fields=['is_connected'])

            event_logger.openstack_subnet.info(
                'SubNet %s has been disconnected from network' % subnet.name,
                event_type='openstack_subnet_updated',
                event_context={
                    'subnet': subnet,
                },
            )

    def connect_subnet(self, subnet):
        try:
            self.connect_router(
                subnet.network.name,
                subnet.backend_id,
                tenant_id=subnet.network.tenant.backend_id,
                network_id=subnet.network.backend_id,
            )
        except neutron_exceptions.NeutronException as e:
            raise OpenStackBackendError(e)
        else:
            subnet.is_connected = True
            subnet.save(update_fields=['is_connected'])

            event_logger.openstack_subnet.info(
                'SubNet %s has been connected to network' % subnet.name,
                event_type='openstack_subnet_updated',
                event_context={
                    'subnet': subnet,
                },
            )

    @log_backend_action()
    def delete_subnet(self, subnet):
        neutron = self.neutron_admin_client
        try:
            self.disconnect_subnet(subnet)
            neutron.delete_subnet(subnet.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            subnet.decrease_backend_quotas_usage()
            event_logger.openstack_subnet.info(
                'SubNet %s has been deleted' % subnet.name,
                event_type='openstack_subnet_deleted',
                event_context={
                    'subnet': subnet,
                },
            )

    def import_subnet(self, subnet_backend_id):
        neutron = self.neutron_admin_client
        try:
            backend_subnet = neutron.show_subnet(subnet_backend_id)['subnet']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        is_connected = self.is_subnet_connected(
            backend_subnet['id'], backend_subnet['network_id']
        )

        return self._backend_subnet_to_subnet(backend_subnet, is_connected=is_connected)

    def is_subnet_connected(self, subnet_backend_id, subnet_network_backend_id):
        neutron = self.neutron_admin_client

        try:
            ports = neutron.list_ports(network_id=subnet_network_backend_id)['ports']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        for port in ports:
            if port['device_owner'] not in VALID_ROUTER_INTERFACE_OWNERS:
                continue
            for fixed_ip in port['fixed_ips']:
                if fixed_ip['subnet_id'] == subnet_backend_id:
                    return True
        return False

    @log_backend_action()
    def pull_subnet(self, subnet):
        import_time = timezone.now()
        imported_subnet = self.import_subnet(subnet.backend_id)

        subnet.refresh_from_db()
        if subnet.modified < import_time:
            modified = update_pulled_fields(
                subnet, imported_subnet, models.SubNet.get_backend_fields()
            )
            if modified:
                event_logger.openstack_subnet.info(
                    'SubNet %s has been pulled from backend.' % subnet.name,
                    event_type='openstack_subnet_pulled',
                    event_context={
                        'subnet': subnet,
                    },
                )

    @log_backend_action('pull floating ip')
    def pull_floating_ip(self, floating_ip):
        neutron = self.neutron_client
        try:
            backend_floating_ip = neutron.show_floatingip(floating_ip.backend_id)[
                'floatingip'
            ]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        imported_floating_ip = self._backend_floating_ip_to_floating_ip(
            backend_floating_ip, tenant=floating_ip.tenant
        )
        update_pulled_fields(
            floating_ip, imported_floating_ip, models.FloatingIP.get_backend_fields()
        )

    @log_backend_action('delete floating ip')
    def delete_floating_ip(self, floating_ip):
        self._delete_backend_floating_ip(
            floating_ip.backend_id, floating_ip.tenant.backend_id
        )
        floating_ip.decrease_backend_quotas_usage()

    @log_backend_action('create floating ip')
    def create_floating_ip(self, floating_ip):
        neutron = self.neutron_client
        try:
            backend_floating_ip = neutron.create_floatingip(
                {
                    'floatingip': {
                        'floating_network_id': floating_ip.tenant.external_network_id,
                        'tenant_id': floating_ip.tenant.backend_id,
                    }
                }
            )['floatingip']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            floating_ip.runtime_state = backend_floating_ip['status']
            floating_ip.address = backend_floating_ip['floating_ip_address']
            floating_ip.name = backend_floating_ip['floating_ip_address']
            floating_ip.backend_id = backend_floating_ip['id']
            floating_ip.backend_network_id = backend_floating_ip['floating_network_id']
            floating_ip.save()

    @log_backend_action()
    def connect_tenant_to_external_network(self, tenant, external_network_id):
        neutron = self.neutron_admin_client
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
                'External network %s does not exist. Stale data in database?',
                external_network_id,
            )
            raise OpenStackBackendError(e)

        network_name = response['network']['name']
        subnet_id = response['network']['subnets'][0]
        self.connect_router(
            network_name, subnet_id, external=True, network_id=response['network']['id']
        )

        tenant.external_network_id = external_network_id
        tenant.save()

        logger.info(
            'Router between external network %s and tenant %s was successfully created',
            external_network_id,
            tenant.backend_id,
        )

        return external_network_id

    def _get_router(self, tenant_id):
        neutron = self.neutron_client

        try:
            routers = neutron.list_routers(tenant_id=tenant_id)['routers']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        # If any router in Tenant exists, use it
        return routers[0] if routers else None

    def _create_router(self, router_name, tenant_id):
        neutron = self.neutron_client
        create_ha_routers = bool(self.settings.options.get('create_ha_routers'))
        options = {
            'router': {
                'name': router_name,
                'tenant_id': tenant_id,
                'ha': create_ha_routers,
            }
        }

        try:
            router = neutron.create_router(options)['router']
            logger.info('Router %s has been created in the backend.', router['name'])
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        return router

    def _connect_network_to_router(
        self, router, external, tenant_id, network_id=None, subnet_id=None
    ):
        neutron = self.neutron_client
        try:
            if external:
                if (
                    not router.get('external_gateway_info')
                    or router['external_gateway_info'].get('network_id') != network_id
                ):
                    backend_router = neutron.add_gateway_router(
                        router['id'], {'network_id': network_id}
                    )['router']
                    external_ip_info = backend_router['external_gateway_info'][
                        'external_fixed_ips'
                    ][0]
                    logger.info(
                        'External network %s has been connected to the router %s with external IP %s within subnet %s.',
                        network_id,
                        router['name'],
                        external_ip_info['ip_address'],
                        external_ip_info['subnet_id'],
                    )
                else:
                    logger.info(
                        'External network %s is already connected to router %s.',
                        network_id,
                        router['name'],
                    )
            else:
                subnet = neutron.show_subnet(subnet_id)['subnet']
                # Subnet for router interface must have a gateway IP.
                if not subnet['gateway_ip']:
                    return
                ports = neutron.list_ports(
                    device_id=router['id'], tenant_id=tenant_id, network_id=network_id
                )['ports']
                if not ports:
                    neutron.add_interface_router(router['id'], {'subnet_id': subnet_id})
                    logger.info(
                        'Internal subnet %s was connected to the router %s.',
                        subnet_id,
                        router['name'],
                    )
                else:
                    logger.info(
                        'Internal subnet %s is already connected to the router %s.',
                        subnet_id,
                        router['name'],
                    )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    def connect_router(
        self, network_name, subnet_id, external=False, network_id=None, tenant_id=None
    ):
        tenant_id = tenant_id or self.tenant_id
        router_name = '{0}-router'.format(network_name)
        router = self._get_router(tenant_id) or self._create_router(
            router_name, tenant_id
        )
        self._connect_network_to_router(
            router, external, tenant_id, network_id, subnet_id
        )

        return router['id']

    @log_backend_action()
    def update_tenant(self, tenant):
        keystone = self.keystone_admin_client
        try:
            keystone.projects.update(
                tenant.backend_id, name=tenant.name, description=tenant.description
            )
        except keystone_exceptions.NotFound as e:
            logger.error('Tenant with id %s does not exist', tenant.backend_id)
            raise OpenStackBackendError(e)

    def pull_service_settings_quotas(self):
        nova = self.nova_admin_client
        try:
            stats = nova.hypervisor_stats.statistics()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        self.settings.set_quota_limit(self.settings.Quotas.openstack_vcpu, stats.vcpus)
        self.settings.set_quota_usage(
            self.settings.Quotas.openstack_vcpu, stats.vcpus_used
        )

        self.settings.set_quota_limit(
            self.settings.Quotas.openstack_ram, stats.memory_mb
        )
        self.settings.set_quota_usage(
            self.settings.Quotas.openstack_ram, stats.memory_mb_used
        )

        self.settings.set_quota_usage(
            self.settings.Quotas.openstack_storage, self.get_storage_usage()
        )

    def get_storage_usage(self):
        cinder = self.cinder_admin_client

        try:
            volumes = cinder.volumes.list()
            snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        storage = sum(self.gb2mb(v.size) for v in volumes + snapshots)
        return storage

    @log_backend_action()
    def create_port(self, port: models.Port, serialized_network: models.Network):
        neutron = self.neutron_admin_client
        network = core_utils.deserialize_instance(serialized_network)

        port_payload = {
            'name': port.name,
            'description': port.description,
            'network_id': network.backend_id,
            'fixed_ips': port.fixed_ips,
            'tenant_id': port.tenant.backend_id,
        }
        if port.mac_address:
            port_payload['mac_address'] = port.mac_address

        try:
            port_response = neutron.create_port({'port': port_payload})['port']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            port.mac_address = port_response['mac_address']
            port.backend_id = port_response['id']
            port.fixed_ips = port_response['fixed_ips']
            port.save(update_fields=['backend_id', 'mac_address', 'fixed_ips'])

            event_logger.opentask_port.info(
                'Port [%s] has been created in the backend for network [%s].'
                % (port, network),
                event_type='openstack_port_created',
                event_context={'port': port},
            )

            return port

    @log_backend_action()
    def delete_port(self, port: models.Port):
        neutron = self.neutron_admin_client

        try:
            neutron.delete_port(port.backend_id)
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            event_logger.openstack_port.info(
                'Port [%s] has been deleted from network [%s].' % (port, port.network),
                event_type='openstack_port_deleted',
                event_context={'port': port},
            )

    @log_backend_action()
    def attach_floating_ip_to_port(
        self, floating_ip: models.FloatingIP, serialized_port
    ):
        port: models.Port = core_utils.deserialize_instance(serialized_port)
        neutron = self.neutron_admin_client
        payload = {
            'port_id': port.backend_id,
        }
        try:
            response_floating_ip = neutron.update_floatingip(
                floating_ip.backend_id, {'floatingip': payload}
            )['floatingip']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            floating_ip.runtime_state = response_floating_ip['status']
            floating_ip.address = response_floating_ip['fixed_ip_address']
            floating_ip.port = port
            floating_ip.save(update_fields=['address', 'runtime_state', 'port'])

            event_logger.openstack_floating_ip.info(
                'Floating IP [%s] has been attached to port [%s].'
                % (floating_ip, port),
                event_type='openstack_floating_ip_attached',
                event_context={
                    'floating_ip': floating_ip,
                    'port': port,
                },
            )

    @log_backend_action()
    def detach_floating_ip_from_port(self, floating_ip: models.FloatingIP):
        neutron = self.neutron_admin_client
        payload = {
            'port_id': None,
        }
        try:
            response_floating_ip = neutron.update_floatingip(
                floating_ip.backend_id, {'floatingip': payload}
            )['floatingip']
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)
        else:
            port = floating_ip.port
            floating_ip.runtime_state = response_floating_ip['status']
            floating_ip.address = None
            floating_ip.port = None
            floating_ip.save(update_fields=['address', 'runtime_state', 'port'])

            event_logger.openstack_floating_ip.info(
                'Floating IP %s has been detached from port %s.' % (floating_ip, port),
                event_type='openstack_floating_ip_detached',
                event_context={
                    'floating_ip': floating_ip,
                    'port': port,
                },
            )

    def _log_server_group_imported(self, server_group):
        event_logger.openstack_server_group.info(
            'Server group %s has been imported to local cache.' % server_group.name,
            event_type='openstack_server_group_imported',
            event_context={'server_group': server_group},
        )

    def _log_server_group_pulled(self, server_group):
        event_logger.openstack_server_group.info(
            'Server group %s has been pulled from backend.' % server_group.name,
            event_type='openstack_server_group_pulled',
            event_context={'server_group': server_group},
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

    def _update_tenant_server_groups(self, tenant, backend_server_groups):
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
                        'Skipping pulling of OpenStack server group because it is '
                        'not in the stable state. Group ID: %s',
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
                'Server group %s has been cleaned from cache.' % server_group.name,
                event_type='openstack_server_group_cleaned',
                event_context={
                    'server_group': server_group,
                },
            )
        stale_groups.delete()

    def list_server_groups(self, tenants):
        nova = self.nova_admin_client
        try:
            list_of_all_server_groups = nova.server_groups.list(all_projects=True)
            return [
                server_group
                for server_group in list_of_all_server_groups
                if server_group.project_id in tenants
            ]
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def pull_server_groups(self, tenants=None):
        if tenants is None:
            tenants = models.Tenant.objects.filter(
                state=models.Tenant.States.OK,
                service_settings=self.settings,
            ).prefetch_related('server_groups')
        tenant_mappings = {tenant.backend_id: tenant for tenant in tenants}
        if not tenant_mappings:
            return

        backend_server_groups = self.list_server_groups(list(tenant_mappings.keys()))
        tenant_server_groups = defaultdict(list)
        for server_group in backend_server_groups:
            tenant_id = server_group.project_id
            tenant = tenant_mappings[tenant_id]
            tenant_server_groups[tenant].append(server_group)

        with transaction.atomic():
            for tenant, server_groups in tenant_server_groups.items():
                self._update_tenant_server_groups(tenant, server_groups)
            self._remove_stale_server_groups(tenants, backend_server_groups)

    def pull_server_group(self, local_server_group: models.ServerGroup):
        nova = self.nova_client
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

    @log_backend_action('pull server groups for tenant')
    def pull_tenant_server_groups(self, tenant):
        nova = self.nova_client
        try:
            backend_server_groups = nova.server_groups.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        with transaction.atomic():
            self._update_tenant_server_groups(tenant, backend_server_groups)
            self._remove_stale_server_groups([tenant], backend_server_groups)
