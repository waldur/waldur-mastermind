import json
import logging
import re

from ceilometerclient import exc as ceilometer_exceptions
from cinderclient import exceptions as cinder_exceptions
from django.db import transaction, IntegrityError
from django.utils import six, timezone, dateparse
from django.utils.functional import cached_property
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_core.structure import log_backend_action
from waldur_core.structure.utils import (
    update_pulled_fields, handle_resource_not_found, handle_resource_update_success)
from waldur_openstack.openstack_base.backend import BaseOpenStackBackend, OpenStackBackendError

from . import models

logger = logging.getLogger(__name__)


def backend_internal_ip_to_internal_ip(backend_internal_ip, **kwargs):
    internal_ip = models.InternalIP(
        backend_id=backend_internal_ip['id'],
        mac_address=backend_internal_ip['mac_address'],
        ip4_address=backend_internal_ip['fixed_ips'][0]['ip_address'],
    )

    for field, value in kwargs.items():
        setattr(internal_ip, field, value)

    if 'instance' not in kwargs:
        internal_ip._instance_backend_id = backend_internal_ip['device_id']
    if 'subnet' not in kwargs:
        internal_ip._subnet_backend_id = backend_internal_ip['fixed_ips'][0]['subnet_id']

    internal_ip._device_owner = backend_internal_ip['device_owner']

    return internal_ip


class InternalIPSynchronizer(object):
    """
    It is assumed that all subnets for the current tenant have been successfully synchronized.
    """

    def __init__(self, neutron_client, tenant_id, settings):
        self.neutron_client = neutron_client
        self.tenant_id = tenant_id
        self.settings = settings

    @cached_property
    def remote_ips(self):
        """
        Fetch all Neutron ports for the current tenant.
        Convert Neutron port to local internal IP model.
        """
        try:
            ips = self.neutron_client.list_ports(tenant_id=self.tenant_id)['ports']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        return [backend_internal_ip_to_internal_ip(ip) for ip in ips]

    @cached_property
    def local_ips(self):
        """
        Prepare mapping from backend ID to local internal IP model.
        """
        internal_ips = models.InternalIP.objects\
            .filter(subnet__settings=self.settings).exclude(backend_id='')
        return {ip.backend_id: ip for ip in internal_ips}

    @cached_property
    def pending_ips(self):
        """
        Prepare mapping from device and subnet ID to local internal IP model.
        """
        pending_internal_ips = models.InternalIP.objects.filter(
            subnet__settings=self.settings, backend_id='').exclude(instance__isnull=True)
        return {(ip.instance.backend_id, ip.subnet.backend_id): ip for ip in pending_internal_ips}

    @cached_property
    def stale_ips(self):
        """
        Prepare list of IDs for stale internal IPs.
        """
        remote_ips = {ip.backend_id for ip in self.remote_ips}
        return [
            ip.pk
            for (backend_id, ip) in self.local_ips.items()
            if backend_id not in remote_ips
        ]

    @cached_property
    def instances(self):
        """
        Prepare mapping from backend ID to local instance model.
        """
        instances = models.Instance.objects.filter(
            service_project_link__service__settings=self.settings).exclude(backend_id='')
        return {instance.backend_id: instance for instance in instances}

    @cached_property
    def subnets(self):
        """
        Prepare mapping from backend ID to local subnet model.
        """
        subnets = models.SubNet.objects.filter(settings=self.settings).exclude(backend_id='')
        return {subnet.backend_id: subnet for subnet in subnets}

    @transaction.atomic
    def execute(self):
        for remote_ip in self.remote_ips:

            # Check if related subnet exists.
            subnet = self.subnets.get(remote_ip._subnet_backend_id)
            if subnet is None:
                logger.warning('Skipping Neutron port synchronization process because '
                               'related subnet is not imported yet. Port ID: %s, subnet ID: %s',
                               remote_ip.backend_id, remote_ip._subnet_backend_id)
                continue

            local_ip = self.local_ips.get(remote_ip.backend_id)
            instance = None

            if remote_ip._device_owner == 'compute:nova':
                instance = self.instances.get(remote_ip._instance_backend_id)
                # Check if internal IP is pending.
                if instance and not local_ip:
                    local_ip = self.pending_ips.get((instance.backend_id, subnet.backend_id))

            # Create local internal IP if it does not exist yet.
            if local_ip is None:
                local_ip = remote_ip
                local_ip.subnet = subnet
                local_ip.instance = instance
                local_ip.save()
            else:
                # Update backend ID for pending internal IP.
                update_pulled_fields(local_ip, remote_ip,
                                     models.InternalIP.get_backend_fields() + ('backend_id',))

        # Remove stale internal IPs.
        if self.stale_ips:
            models.InternalIP.objects.filter(pk__in=self.stale_ips).delete()


class OpenStackTenantBackend(BaseOpenStackBackend):

    def __init__(self, settings):
        super(OpenStackTenantBackend, self).__init__(settings, settings.options['tenant_id'])

    @property
    def external_network_id(self):
        return self.settings.options['external_network_id']

    def sync(self):
        # pull service properties
        self.pull_flavors()
        self.pull_images()
        self.pull_security_groups()
        self.pull_quotas()
        self.pull_networks()
        self.pull_subnets()
        self.pull_internal_ips()
        self.pull_floating_ips()

        # pull resources
        self.pull_volumes()
        self.pull_snapshots()
        self.pull_instances()

    def pull_volumes(self):
        backend_volumes = self.get_volumes()
        volumes = models.Volume.objects.filter(
            service_project_link__service__settings=self.settings,
            state__in=[models.Volume.States.OK, models.Volume.States.ERRED]
        )
        backend_volumes_map = {backend_volume.backend_id: backend_volume for backend_volume in backend_volumes}
        for volume in volumes:
            try:
                backend_volume = backend_volumes_map[volume.backend_id]
            except KeyError:
                handle_resource_not_found(volume)
            else:
                update_pulled_fields(volume, backend_volume, models.Volume.get_backend_fields())
                handle_resource_update_success(volume)

    def pull_snapshots(self):
        backend_snapshots = self.get_snapshots()
        snapshots = models.Snapshot.objects.filter(
            service_project_link__service__settings=self.settings,
            state__in=[models.Snapshot.States.OK, models.Snapshot.States.ERRED])
        backend_snapshots_map = {backend_snapshot.backend_id: backend_snapshot
                                 for backend_snapshot in backend_snapshots}
        for snapshot in snapshots:
            try:
                backend_snapshot = backend_snapshots_map[snapshot.backend_id]
            except KeyError:
                handle_resource_not_found(snapshot)
            else:
                update_pulled_fields(snapshot, backend_snapshot, models.Snapshot.get_backend_fields())
                handle_resource_update_success(snapshot)

    def pull_instances(self):
        backend_instances = self.get_instances()
        instances = models.Instance.objects.filter(
            service_project_link__service__settings=self.settings,
            state__in=[models.Instance.States.OK, models.Instance.States.ERRED],
        )
        backend_instances_map = {backend_instance.backend_id: backend_instance
                                 for backend_instance in backend_instances}
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

    def update_instance_fields(self, instance, backend_instance):
        # Preserve flavor fields in Waldur database if flavor is deleted in OpenStack
        fields = set(models.Instance.get_backend_fields())
        flavor_fields = {'flavor_name', 'flavor_disk', 'ram', 'cores', 'disk'}
        if not backend_instance.flavor_name:
            fields = fields - flavor_fields
        fields = list(fields)

        update_pulled_fields(instance, backend_instance, fields)

    def pull_flavors(self):
        nova = self.nova_client
        try:
            flavors = nova.flavors.findall()
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

        flavor_exclude_regex = self.settings.options.get('flavor_exclude_regex', '')
        name_pattern = re.compile(flavor_exclude_regex) if flavor_exclude_regex else None
        with transaction.atomic():
            cur_flavors = self._get_current_properties(models.Flavor)
            for backend_flavor in flavors:
                if name_pattern is not None and name_pattern.match(backend_flavor.name) is not None:
                    logger.debug('Skipping pull of %s flavor as it matches %s regex pattern.',
                                 backend_flavor.name, flavor_exclude_regex)
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
                    })

            models.Flavor.objects.filter(backend_id__in=cur_flavors.keys(), settings=self.settings).delete()

    def pull_images(self):
        self._pull_images(models.Image)

    def pull_floating_ips(self):
        # method assumes that instance internal IPs is up to date.
        neutron = self.neutron_client

        try:
            backend_floating_ips = neutron.list_floatingips(tenant_id=self.tenant_id)['floatingips']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        # Step 1. Prepare data
        imported_ips = {ip.backend_id: ip
                        for ip in (self._backend_floating_ip_to_floating_ip(ip)
                                   for ip in backend_floating_ips)}

        floating_ips = {ip.backend_id: ip for ip in models.FloatingIP.objects.filter(
            settings=self.settings).exclude(backend_id='')}
        booked_ips = [backend_id for backend_id, ip in floating_ips.items() if ip.is_booked]

        # all imported IPs except those which are booked.
        ips_to_update = set(imported_ips) - set(booked_ips)

        internal_ips = {ip.backend_id: ip for ip in models.InternalIP.objects.filter(
            subnet__settings=self.settings).exclude(backend_id='')}

        # Step 2. Update or create imported IPs
        for backend_id in ips_to_update:
            imported_ip = imported_ips[backend_id]
            floating_ip = floating_ips.get(backend_id)

            internal_ip = internal_ips.get(imported_ip._internal_ip_backend_id)
            if imported_ip._internal_ip_backend_id and internal_ip is None:
                logger.warning('Failed to set internal_ip for Floating IP %s', imported_ip.backend_id)
                continue

            imported_ip.internal_ip = internal_ip
            if not floating_ip:
                imported_ip.save()
            else:
                fields_to_update = models.FloatingIP.get_backend_fields() + ('internal_ip',)
                if floating_ip.address != floating_ip.name:
                    # Don't update user defined name.
                    fields_to_update = [field for field in fields_to_update if field != 'name']

                update_pulled_fields(floating_ip, imported_ip, fields_to_update)

        # Step 3. Delete stale IPs
        ips_to_delete = set(floating_ips) - set(imported_ips)
        models.FloatingIP.objects.filter(settings=self.settings,
                                         backend_id__in=ips_to_delete).delete()

    def _backend_floating_ip_to_floating_ip(self, backend_floating_ip, **kwargs):
        floating_ip = models.FloatingIP(
            settings=self.settings,
            name=backend_floating_ip['floating_ip_address'],
            address=backend_floating_ip['floating_ip_address'],
            backend_network_id=backend_floating_ip['floating_network_id'],
            runtime_state=backend_floating_ip['status'],
            backend_id=backend_floating_ip['id'],
        )
        for field, value in kwargs.items():
            setattr(floating_ip, field, value)

        if 'internal_ip' not in kwargs:
            floating_ip._internal_ip_backend_id = backend_floating_ip['port_id']

        return floating_ip

    def _delete_stale_properties(self, model, backend_items):
        with transaction.atomic():
            current_items = model.objects.filter(settings=self.settings)
            current_ids = set(current_items.values_list('backend_id', flat=True))
            backend_ids = set(item['id'] for item in backend_items)
            stale_ids = current_ids - backend_ids
            if stale_ids:
                model.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def pull_security_groups(self):
        neutron = self.neutron_client
        try:
            security_groups = neutron.list_security_groups(tenant_id=self.tenant_id)['security_groups']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        for backend_security_group in security_groups:
            backend_id = backend_security_group['id']
            defaults = {
                'name': backend_security_group['name'],
                'description': backend_security_group['description']
            }
            try:
                with transaction.atomic():
                    security_group, _ = models.SecurityGroup.objects.update_or_create(
                        settings=self.settings,
                        backend_id=backend_id,
                        defaults=defaults
                    )
                    self._extract_security_group_rules(security_group, backend_security_group)
            except IntegrityError:
                logger.warning('Could not create security group with backend ID %s '
                               'and service settings %s due to concurrent update.',
                               backend_id, self.settings)

        self._delete_stale_properties(models.SecurityGroup, security_groups)

    def pull_quotas(self):
        self._pull_tenant_quotas(self.tenant_id, self.settings)

    def pull_networks(self):
        neutron = self.neutron_client
        try:
            networks = neutron.list_networks(tenant_id=self.tenant_id)['networks']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        for backend_network in networks:
            defaults = {
                'name': backend_network['name'],
                'description': backend_network['description'],
            }
            if backend_network.get('provider:network_type'):
                defaults['type'] = backend_network['provider:network_type']
            if backend_network.get('provider:segmentation_id'):
                defaults['segmentation_id'] = backend_network['provider:segmentation_id']
            backend_id = backend_network['id']
            try:
                models.Network.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_id,
                    defaults=defaults
                )
            except IntegrityError:
                logger.warning('Could not create network with backend ID %s '
                               'and service settings %s due to concurrent update.',
                               backend_id, self.settings)

        self._delete_stale_properties(models.Network, networks)

    def pull_subnets(self):
        neutron = self.neutron_client
        try:
            subnets = neutron.list_subnets(tenant_id=self.tenant_id)['subnets']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        current_networks = {
            network.backend_id: network.id
            for network in models.Network.objects.filter(settings=self.settings).only('id', 'backend_id')
        }
        subnet_networks = set(subnet['network_id'] for subnet in subnets)
        missing_networks = subnet_networks - set(current_networks.keys())
        if missing_networks:
            logger.warning('Cannot pull subnets for networks with id %s '
                           'because their networks are not pulled yet.', missing_networks)

        for backend_subnet in subnets:
            backend_id = backend_subnet['id']
            network_id = current_networks.get(backend_subnet['network_id'])
            if not network_id:
                continue
            defaults = {
                'name': backend_subnet['name'],
                'description': backend_subnet['description'],
                'allocation_pools': backend_subnet['allocation_pools'],
                'cidr': backend_subnet['cidr'],
                'ip_version': backend_subnet['ip_version'],
                'gateway_ip': backend_subnet.get('gateway_ip'),
                'enable_dhcp': backend_subnet.get('enable_dhcp', False),
                'network_id': network_id,
            }
            try:
                models.SubNet.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_id,
                    defaults=defaults
                )
            except IntegrityError:
                logger.warning('Could not create subnet with backend ID %s '
                               'and service settings %s due to concurrent update.',
                               backend_id, self.settings)

        self._delete_stale_properties(models.SubNet, subnets)

    @log_backend_action()
    def create_volume(self, volume):
        kwargs = {
            'size': self.mb2gb(volume.size),
            'name': volume.name,
            'description': volume.description,
        }
        if volume.source_snapshot:
            kwargs['snapshot_id'] = volume.source_snapshot.backend_id
        if volume.type:
            kwargs['volume_type'] = volume.type
        if volume.image:
            kwargs['imageRef'] = volume.image.backend_id
        cinder = self.cinder_client
        try:
            backend_volume = cinder.volumes.create(**kwargs)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        volume.backend_id = backend_volume.id
        if hasattr(backend_volume, 'volume_image_metadata'):
            volume.image_metadata = backend_volume.volume_image_metadata
        volume.bootable = backend_volume.bootable == 'true'
        volume.runtime_state = backend_volume.status
        volume.save()
        return volume

    @log_backend_action()
    def update_volume(self, volume):
        cinder = self.cinder_client
        try:
            cinder.volumes.update(volume.backend_id, name=volume.name, description=volume.description)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def delete_volume(self, volume):
        cinder = self.cinder_client
        try:
            cinder.volumes.delete(volume.backend_id)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        volume.decrease_backend_quotas_usage()

    @log_backend_action()
    def attach_volume(self, volume, instance_uuid, device=None):
        instance = models.Instance.objects.get(uuid=instance_uuid)
        nova = self.nova_client
        try:
            nova.volumes.create_server_volume(instance.backend_id, volume.backend_id,
                                              device=None if device == '' else device)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        else:
            volume.instance = instance
            volume.device = device
            volume.save(update_fields=['instance', 'device'])

    @log_backend_action()
    def detach_volume(self, volume):
        nova = self.nova_client
        try:
            nova.volumes.delete_server_volume(volume.instance.backend_id, volume.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        else:
            volume.instance = None
            volume.device = ''
            volume.save(update_fields=['instance', 'device'])

    @log_backend_action()
    def extend_volume(self, volume):
        cinder = self.cinder_client
        try:
            cinder.volumes.extend(volume.backend_id, self.mb2gb(volume.size))
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    def import_volume(self, backend_volume_id, save=True, service_project_link=None):
        """ Restore Waldur volume instance based on backend data. """
        cinder = self.cinder_client
        try:
            backend_volume = cinder.volumes.get(backend_volume_id)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        volume = self._backend_volume_to_volume(backend_volume)
        if service_project_link is not None:
            volume.service_project_link = service_project_link
        if save:
            volume.save()

        return volume

    def _backend_volume_to_volume(self, backend_volume):
        volume = models.Volume(
            name=backend_volume.name,
            description=backend_volume.description or '',
            size=self.gb2mb(backend_volume.size),
            metadata=backend_volume.metadata,
            backend_id=backend_volume.id,
            type=backend_volume.volume_type or '',
            bootable=backend_volume.bootable == 'true',
            runtime_state=backend_volume.status,
            state=models.Volume.States.OK,
        )
        if getattr(backend_volume, 'volume_image_metadata', False):
            volume.image_metadata = backend_volume.volume_image_metadata
            try:
                volume.image = models.Image.objects.get(
                    settings=self.settings, backend_id=volume.image_metadata['image_id'])
            except models.Image.DoesNotExist:
                pass
        # In our setup volume could be attached only to one instance.
        if getattr(backend_volume, 'attachments', False):
            if 'device' in backend_volume.attachments[0]:
                volume.device = backend_volume.attachments[0]['device']

            if 'server_id' in backend_volume.attachments[0]:
                volume.instance = models.Instance.objects.filter(
                    service_project_link__service__settings=self.settings,
                    backend_id=backend_volume.attachments[0]['server_id'],
                ).first()
        return volume

    def get_volumes(self):
        cinder = self.cinder_client
        try:
            backend_volumes = cinder.volumes.list()
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        return [self._backend_volume_to_volume(backend_volume) for backend_volume in backend_volumes]

    def get_volumes_for_import(self):
        return self._get_backend_resource(models.Volume, self.get_volumes())

    @log_backend_action()
    def remove_bootable_flag(self, volume):
        cinder = self.cinder_client
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
            cinder.volumes.set_bootable(backend_volume, False)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        volume.bootable = False
        volume.save(update_fields=['bootable'])

    @log_backend_action()
    def pull_volume(self, volume, update_fields=None):
        import_time = timezone.now()
        imported_volume = self.import_volume(volume.backend_id, save=False)

        volume.refresh_from_db()
        if volume.modified < import_time:
            if not update_fields:
                update_fields = models.Volume.get_backend_fields()

            update_pulled_fields(volume, imported_volume, update_fields)

    @log_backend_action()
    def pull_volume_runtime_state(self, volume):
        cinder = self.cinder_client
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        if backend_volume.status != volume.runtime_state:
            volume.runtime_state = backend_volume.status
            volume.save(update_fields=['runtime_state'])

    @log_backend_action('check is volume deleted')
    def is_volume_deleted(self, volume):
        cinder = self.cinder_client
        try:
            cinder.volumes.get(volume.backend_id)
            return False
        except cinder_exceptions.NotFound:
            return True
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def create_snapshot(self, snapshot, force=True):
        kwargs = {
            'name': snapshot.name,
            'description': snapshot.description,
            'force': force,
            'metadata': snapshot.metadata,
        }
        cinder = self.cinder_client
        try:
            backend_snapshot = cinder.volume_snapshots.create(snapshot.source_volume.backend_id, **kwargs)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        snapshot.backend_id = backend_snapshot.id
        snapshot.runtime_state = backend_snapshot.status
        snapshot.size = self.gb2mb(backend_snapshot.size)
        snapshot.save()
        return snapshot

    def import_snapshot(self, backend_snapshot_id, save=True, service_project_link=None):
        """ Restore Waldur Snapshot instance based on backend data. """
        cinder = self.cinder_client
        try:
            backend_snapshot = cinder.volume_snapshots.get(backend_snapshot_id)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        snapshot = self._backend_snapshot_to_snapshot(backend_snapshot)
        if service_project_link is not None:
            snapshot.service_project_link = service_project_link
        if save:
            snapshot.save()
        return snapshot

    def _backend_snapshot_to_snapshot(self, backend_snapshot):
        snapshot = models.Snapshot(
            name=backend_snapshot.name,
            description=backend_snapshot.description or '',
            size=self.gb2mb(backend_snapshot.size),
            metadata=backend_snapshot.metadata,
            backend_id=backend_snapshot.id,
            runtime_state=backend_snapshot.status,
            state=models.Snapshot.States.OK,
        )
        if hasattr(backend_snapshot, 'volume_id'):
            snapshot.source_volume = models.Volume.objects.filter(
                service_project_link__service__settings=self.settings, backend_id=backend_snapshot.volume_id).first()
        return snapshot

    def get_snapshots(self):
        cinder = self.cinder_client
        try:
            backend_snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        return [self._backend_snapshot_to_snapshot(backend_snapshot) for backend_snapshot in backend_snapshots]

    def get_snapshots_for_import(self):
        return self._get_backend_resource(models.Snapshot, self.get_snapshots())

    @log_backend_action()
    def pull_snapshot(self, snapshot, update_fields=None):
        import_time = timezone.now()
        imported_snapshot = self.import_snapshot(snapshot.backend_id, save=False)

        snapshot.refresh_from_db()
        if snapshot.modified < import_time:
            if update_fields is None:
                update_fields = models.Snapshot.get_backend_fields()
            update_pulled_fields(snapshot, imported_snapshot, update_fields)

    @log_backend_action()
    def pull_snapshot_runtime_state(self, snapshot):
        cinder = self.cinder_client
        try:
            backend_snapshot = cinder.volume_snapshots.get(snapshot.backend_id)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        if backend_snapshot.status != snapshot.runtime_state:
            snapshot.runtime_state = backend_snapshot.status
            snapshot.save(update_fields=['runtime_state'])
        return snapshot

    @log_backend_action()
    def delete_snapshot(self, snapshot):
        cinder = self.cinder_client
        try:
            cinder.volume_snapshots.delete(snapshot.backend_id)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        snapshot.decrease_backend_quotas_usage()

    @log_backend_action()
    def update_snapshot(self, snapshot):
        cinder = self.cinder_client
        try:
            cinder.volume_snapshots.update(
                snapshot.backend_id, name=snapshot.name, description=snapshot.description)
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action('check is snapshot deleted')
    def is_snapshot_deleted(self, snapshot):
        cinder = self.cinder_client
        try:
            cinder.volume_snapshots.get(snapshot.backend_id)
            return False
        except cinder_exceptions.NotFound:
            return True
        except cinder_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def create_instance(self, instance, backend_flavor_id=None, public_key=None):
        nova = self.nova_client

        try:
            backend_flavor = nova.flavors.get(backend_flavor_id)

            # instance key name and fingerprint are optional
            # it is assumed that if public_key is specified, then
            # key_name and key_fingerprint have valid values
            if public_key:
                backend_public_key = self._get_or_create_ssh_key(
                    instance.key_name, instance.key_fingerprint, public_key)
            else:
                backend_public_key = None

            try:
                instance.volumes.get(bootable=True)
            except models.Volume.DoesNotExist:
                raise OpenStackBackendError(
                    'Current installation cannot create instance without a system volume.')

            security_group_ids = instance.security_groups.values_list('backend_id', flat=True)
            internal_ips = instance.internal_ips_set.all()
            network_ids = [{'net-id': internal_ip.subnet.network.backend_id} for internal_ip in internal_ips]

            block_device_mapping_v2 = []
            for volume in instance.volumes.iterator():
                device_mapping = {
                    'destination_type': 'volume',
                    'device_type': 'disk',
                    'source_type': 'volume',
                    'uuid': volume.backend_id,
                    'delete_on_termination': True,
                }
                if volume.bootable:
                    device_mapping.update({'boot_index': 0})

                block_device_mapping_v2.append(device_mapping)

            server_create_parameters = dict(
                name=instance.name,
                image=None,  # Boot from volume, see boot_index above
                flavor=backend_flavor,
                block_device_mapping_v2=block_device_mapping_v2,
                nics=network_ids,
                key_name=backend_public_key.name if backend_public_key is not None else None,
                security_groups=security_group_ids,
            )
            availability_zone = self.settings.options.get('availability_zone')
            if availability_zone:
                server_create_parameters['availability_zone'] = availability_zone
            if instance.user_data:
                server_create_parameters['userdata'] = instance.user_data

            server = nova.servers.create(**server_create_parameters)
            instance.backend_id = server.id
            instance.save()
        except nova_exceptions.ClientException as e:
            logger.exception("Failed to provision instance %s", instance.uuid)
            six.reraise(OpenStackBackendError, e)
        else:
            logger.info("Successfully provisioned instance %s", instance.uuid)

    @log_backend_action()
    def pull_instance_floating_ips(self, instance):
        # method assumes that instance internal IPs is up to date.
        neutron = self.neutron_client

        internal_ip_mappings = {ip.backend_id: ip for ip in instance.internal_ips_set.all().exclude(backend_id='')}
        try:
            backend_floating_ips = neutron.list_floatingips(
                tenant_id=self.tenant_id, port_id=internal_ip_mappings.keys())['floatingips']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        floating_ip_mappings = {ip.backend_id: ip for ip in
                                instance.floating_ips.filter(is_booked=False).exclude(backend_id='')}

        with transaction.atomic():
            for backend_floating_ip in backend_floating_ips:
                imported_floating_ip = self._backend_floating_ip_to_floating_ip(backend_floating_ip)

                floating_ip = floating_ip_mappings.pop(imported_floating_ip.backend_id, None)
                if floating_ip is None:
                    internal_ip = internal_ip_mappings[imported_floating_ip._internal_ip_backend_id]
                    imported_floating_ip.internal_ip = internal_ip
                    imported_floating_ip.save()
                    continue

                # Don't update user defined name.
                if floating_ip.address != floating_ip.name:
                    imported_floating_ip.name = floating_ip.name
                update_pulled_fields(floating_ip, imported_floating_ip, models.FloatingIP.get_backend_fields())

            # Detach floating IPs from internal IPs
            instance.floating_ips.filter(backend_id__in=floating_ip_mappings.keys()).update(internal_ip=None)

    @log_backend_action()
    def push_instance_floating_ips(self, instance):
        neutron = self.neutron_client
        instance_floating_ips = instance.floating_ips
        try:
            backend_floating_ips = neutron.list_floatingips(
                port_id=instance.internal_ips_set.values_list('backend_id', flat=True))['floatingips']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        # disconnect stale
        instance_floating_ips_ids = [fip.backend_id for fip in instance_floating_ips]
        for backend_floating_ip in backend_floating_ips:
            if backend_floating_ip['id'] not in instance_floating_ips_ids:
                try:
                    neutron.update_floatingip(backend_floating_ip['id'], body={'floatingip': {'port_id': None}})
                except neutron_exceptions.NeutronClientException as e:
                    six.reraise(OpenStackBackendError, e)

        # connect new ones
        backend_floating_ip_ids = {fip['id']: fip for fip in backend_floating_ips}
        for floating_ip in instance_floating_ips:
            backend_floating_ip = backend_floating_ip_ids.get(floating_ip.backend_id)
            if not backend_floating_ip or backend_floating_ip['port_id'] != floating_ip.internal_ip.backend_id:
                try:
                    neutron.update_floatingip(
                        floating_ip.backend_id, body={'floatingip': {'port_id': floating_ip.internal_ip.backend_id}})
                except neutron_exceptions.NeutronClientException as e:
                    six.reraise(OpenStackBackendError, e)

    def create_floating_ip(self, floating_ip):
        neutron = self.neutron_client
        try:
            backend_floating_ip = neutron.create_floatingip({
                'floatingip': {
                    'floating_network_id': floating_ip.backend_network_id,
                    'tenant_id': self.tenant_id,
                }
            })['floatingip']
        except neutron_exceptions.NeutronClientException as e:
            floating_ip.runtime_state = 'ERRED'
            floating_ip.save()
            six.reraise(OpenStackBackendError, e)
        else:
            floating_ip.address = backend_floating_ip['floating_ip_address']
            floating_ip.backend_id = backend_floating_ip['id']
            floating_ip.save()

    @log_backend_action()
    def delete_floating_ip(self, floating_ip):
        self._delete_backend_floating_ip(floating_ip.backend_id, self.tenant_id)
        floating_ip.decrease_backend_quotas_usage()
        floating_ip.delete()

    @log_backend_action()
    def pull_floating_ip_runtime_state(self, floating_ip):
        neutron = self.neutron_client
        try:
            backend_floating_ip = neutron.show_floatingip(floating_ip.backend_id)['floatingip']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)
        else:
            floating_ip.runtime_state = backend_floating_ip['status']
            floating_ip.save()

    def _get_or_create_ssh_key(self, key_name, fingerprint, public_key):
        nova = self.nova_client

        try:
            return nova.keypairs.find(fingerprint=fingerprint)
        except nova_exceptions.NotFound:
            # Fine, it's a new key, let's add it
            try:
                logger.info('Propagating ssh public key %s to backend', key_name)
                return nova.keypairs.create(name=key_name, public_key=public_key)
            except nova_exceptions.ClientException as e:
                logger.error('Unable to import SSH public key to OpenStack, '
                             'key_name: %s, fingerprint: %s, public_key: %s, error: %s',
                             key_name, fingerprint, public_key, e)
                six.reraise(OpenStackBackendError, e)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def update_instance(self, instance):
        nova = self.nova_client
        try:
            nova.servers.update(instance.backend_id, name=instance.name)
        except keystone_exceptions.NotFound as e:
            six.reraise(OpenStackBackendError, e)

    def import_instance(self, backend_instance_id, save=True, service_project_link=None):
        # NB! This method does not import instance sub-objects like security groups or internal IPs.
        #     They have to be pulled separately.
        nova = self.nova_client
        try:
            backend_instance = nova.servers.get(backend_instance_id)
            flavor = nova.flavors.get(backend_instance.flavor['id'])
            attached_volume_ids = [v.volumeId for v in nova.volumes.get_server_volumes(backend_instance_id)]
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

        instance = self._backend_instance_to_instance(backend_instance, flavor)
        with transaction.atomic():
            # import instance volumes, or use existed if they already exist in Waldur.
            volumes = []
            for backend_volume_id in attached_volume_ids:
                try:
                    volumes.append(models.Volume.objects.get(
                        service_project_link__service__settings=self.settings, backend_id=backend_volume_id))
                except models.Volume.DoesNotExist:
                    volumes.append(self.import_volume(backend_volume_id, save, service_project_link))

            if service_project_link:
                instance.service_project_link = service_project_link
            if hasattr(backend_instance, 'fault'):
                instance.error_message = backend_instance.fault['message']
            if save:
                instance.save()
                instance.volumes.add(*volumes)

        return instance

    def _backend_instance_to_instance(self, backend_instance, backend_flavor=None):
        # parse launch time
        try:
            d = dateparse.parse_datetime(backend_instance.to_dict()['OS-SRV-USG:launched_at'])
        except (KeyError, ValueError, TypeError):
            launch_time = None
        else:
            # At the moment OpenStack does not provide any timezone info,
            # but in future it might do.
            if timezone.is_naive(d):
                launch_time = timezone.make_aware(d, timezone.utc)

        instance = models.Instance(
            name=backend_instance.name or backend_instance.id,
            key_name=backend_instance.key_name or '',
            start_time=launch_time,
            state=models.Instance.States.OK,
            runtime_state=backend_instance.status,
            created=dateparse.parse_datetime(backend_instance.created),
            backend_id=backend_instance.id,
        )

        if backend_flavor:
            instance.flavor_name = backend_flavor.name
            instance.flavor_disk = backend_flavor.disk
            instance.cores = backend_flavor.vcpus
            instance.ram = backend_flavor.ram

        return instance

    def _get_backend_resource(self, model, resources):
        registered_backend_ids = model.objects.filter(
            service_project_link__service__settings=self.settings).values_list('backend_id', flat=True)
        return [instance for instance in resources if instance.backend_id not in registered_backend_ids]

    def get_instances(self):
        nova = self.nova_client
        try:
            backend_instances = nova.servers.list()
            backend_flavors = nova.flavors.list()
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

        backend_flavors_map = {flavor.id: flavor for flavor in backend_flavors}
        instances = []
        for backend_instance in backend_instances:
            instance_flavor = backend_flavors_map.get(backend_instance.flavor['id'])
            instances.append(self._backend_instance_to_instance(backend_instance, instance_flavor))
        return instances

    def get_instances_for_import(self):
        return self._get_backend_resource(models.Instance, self.get_instances())

    @log_backend_action()
    def pull_instance(self, instance, update_fields=None):
        import_time = timezone.now()
        imported_instance = self.import_instance(instance.backend_id, save=False)

        instance.refresh_from_db()
        if instance.modified < import_time:
            if update_fields is None:
                update_fields = models.Instance.get_backend_fields()
            update_pulled_fields(instance, imported_instance, update_fields)

    @log_backend_action()
    def pull_created_instance_internal_ips(self, instance):
        """
        This method updates already existing internal IPs of the instance
        which where created in advance during instance provisioning.
        """
        neutron = self.neutron_client
        try:
            backend_internal_ips = neutron.list_ports(device_id=instance.backend_id)['ports']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        subnet_to_ip_mappings = {ip.subnet.backend_id: ip for ip in instance.internal_ips_set.all()}

        for backend_ip in backend_internal_ips:
            imported_internal_ip = backend_internal_ip_to_internal_ip(backend_ip)
            internal_ip = subnet_to_ip_mappings.get(imported_internal_ip._subnet_backend_id)
            if internal_ip is None:
                logger.warning('Internal IP object does not exist in database for instance %s '
                               'in subnet with backend ID %s', instance.uuid, imported_internal_ip._subnet_backend_id)
            else:
                update_pulled_fields(internal_ip, imported_internal_ip,
                                     models.InternalIP.get_backend_fields() + ('backend_id',))

    @log_backend_action()
    def pull_instance_internal_ips(self, instance):
        # we assume that instance can be connected to subnet only once.
        neutron = self.neutron_client
        try:
            backend_internal_ips = neutron.list_ports(device_id=instance.backend_id)['ports']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        internal_ip_mappings = {ip.subnet.backend_id: ip for ip in instance.internal_ips_set.all()}
        subnet_mappings = {subnet.backend_id: subnet for subnet in models.SubNet.objects.filter(settings=self.settings)}

        with transaction.atomic():
            for backend_internal_ip in backend_internal_ips:
                imported_internal_ip = backend_internal_ip_to_internal_ip(backend_internal_ip, instance=instance)
                subnet = subnet_mappings.get(imported_internal_ip._subnet_backend_id)
                if subnet is None:
                    logger.warning('Skipping Neutron port synchronization process because '
                                   'related subnet is not imported yet. Port ID: %s, subnet ID: %s',
                                   imported_internal_ip.backend_id, imported_internal_ip._subnet_backend_id)
                    continue

                internal_ip = internal_ip_mappings.pop(subnet.backend_id, None)
                if internal_ip is None:
                    internal_ip = imported_internal_ip
                    internal_ip.subnet = subnet
                    internal_ip.instance = instance
                    internal_ip.save()
                else:
                    # Update backend ID for pending internal IP
                    update_pulled_fields(internal_ip, imported_internal_ip,
                                         models.InternalIP.get_backend_fields() + ('backend_id',))

            if internal_ip_mappings:
                # remove stale internal IPs
                instance.internal_ips_set.filter(subnet__backend_id__in=internal_ip_mappings.keys()).delete()

    def pull_internal_ips(self):
        synchronizer = InternalIPSynchronizer(self.neutron_client, self.tenant_id, self.settings)
        synchronizer.execute()

    @log_backend_action()
    def push_instance_internal_ips(self, instance):
        # we assume that instance can be connected to subnet only once
        # we assume that internal IP subnet cannot be changed
        neutron = self.neutron_client
        try:
            backend_internal_ips = neutron.list_ports(device_id=instance.backend_id)['ports']
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)

        # delete stale internal IPs
        exist_ids = instance.internal_ips_set.values_list('backend_id', flat=True)
        for backend_internal_ip in backend_internal_ips:
            if backend_internal_ip['id'] not in exist_ids:
                try:
                    neutron.delete_port(backend_internal_ip['id'])
                except neutron_exceptions.NeutronClientException as e:
                    six.reraise(OpenStackBackendError, e)

        # create new internal IPs
        new_internal_ips = instance.internal_ips_set.exclude(backend_id__in=[ip['id'] for ip in backend_internal_ips])
        for new_internal_ip in new_internal_ips:
            try:
                backend_internal_ip = neutron.create_port({'port': {
                    'network_id': new_internal_ip.subnet.network.backend_id,
                    'device_id': instance.backend_id}
                })['port']
            except neutron_exceptions.NeutronClientException as e:
                six.reraise(OpenStackBackendError, e)
            new_internal_ip.mac_address = backend_internal_ip['mac_address']
            new_internal_ip.ip4_address = backend_internal_ip['fixed_ips'][0]['ip_address']
            new_internal_ip.backend_id = backend_internal_ip['id']
            new_internal_ip.save()

    @log_backend_action()
    def pull_instance_security_groups(self, instance):
        nova = self.nova_client
        server_id = instance.backend_id
        try:
            backend_groups = nova.servers.list_security_group(server_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

        backend_ids = set(g.id for g in backend_groups)
        nc_ids = set(
            models.SecurityGroup.objects
            .filter(instances=instance)
            .exclude(backend_id='')
            .values_list('backend_id', flat=True)
        )

        # remove stale groups
        stale_groups = models.SecurityGroup.objects.filter(backend_id__in=(nc_ids - backend_ids))
        instance.security_groups.remove(*stale_groups)

        # add missing groups
        for group_id in backend_ids - nc_ids:
            try:
                security_group = models.SecurityGroup.objects.get(settings=self.settings, backend_id=group_id)
            except models.SecurityGroup.DoesNotExist:
                logger.exception(
                    'Security group with id %s does not exist at Waldur. Tenant : %s' % (group_id, instance.tenant))
            else:
                instance.security_groups.add(security_group)

    @log_backend_action()
    def push_instance_security_groups(self, instance):
        nova = self.nova_client
        server_id = instance.backend_id
        try:
            backend_ids = set(g.id for g in nova.servers.list_security_group(server_id))
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

        nc_ids = set(
            models.SecurityGroup.objects
            .filter(instances=instance)
            .exclude(backend_id='')
            .values_list('backend_id', flat=True)
        )

        # remove stale groups
        for group_id in backend_ids - nc_ids:
            try:
                nova.servers.remove_security_group(server_id, group_id)
            except nova_exceptions.ClientException:
                logger.exception('Failed to remove security group %s from instance %s', group_id, server_id)
            else:
                logger.info('Removed security group %s from instance %s', group_id, server_id)

        # add missing groups
        for group_id in nc_ids - backend_ids:
            try:
                nova.servers.add_security_group(server_id, group_id)
            except nova_exceptions.ClientException:
                logger.exception('Failed to add security group %s to instance %s', group_id, server_id)
            else:
                logger.info('Added security group %s to instance %s', group_id, server_id)

    @log_backend_action()
    def delete_instance(self, instance):
        nova = self.nova_client
        try:
            nova.servers.delete(instance.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        instance.decrease_backend_quotas_usage()
        for volume in instance.volumes.all():
            volume.decrease_backend_quotas_usage()

    @log_backend_action('check is instance deleted')
    def is_instance_deleted(self, instance):
        nova = self.nova_client
        try:
            nova.servers.get(instance.backend_id)
            return False
        except nova_exceptions.NotFound:
            return True
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def start_instance(self, instance):
        nova = self.nova_client
        try:
            nova.servers.start(instance.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def stop_instance(self, instance):
        nova = self.nova_client
        try:
            nova.servers.stop(instance.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        else:
            instance.start_time = None
            instance.save(update_fields=['start_time'])

    @log_backend_action()
    def restart_instance(self, instance):
        nova = self.nova_client
        try:
            nova.servers.reboot(instance.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def resize_instance(self, instance, flavor_id):
        nova = self.nova_client
        try:
            nova.servers.resize(instance.backend_id, flavor_id, 'MANUAL')
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def pull_instance_runtime_state(self, instance):
        nova = self.nova_client
        try:
            backend_instance = nova.servers.get(instance.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)
        if backend_instance.status != instance.runtime_state:
            instance.runtime_state = backend_instance.status
            instance.save(update_fields=['runtime_state'])

        if hasattr(backend_instance, 'fault'):
            error_message = backend_instance.fault['message']
            if instance.error_message != error_message:
                instance.error_message = error_message
                instance.save(update_fields=['error_message'])

    @log_backend_action()
    def confirm_instance_resize(self, instance):
        nova = self.nova_client
        try:
            nova.servers.confirm_resize(instance.backend_id)
        except nova_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

    @log_backend_action()
    def list_meters(self, resource):
        try:
            file_name = self._get_meters_file_name(resource.__class__)
            with open(file_name) as meters_file:
                meters = json.load(meters_file)
        except (KeyError, IOError):
            raise OpenStackBackendError("Cannot find meters for the '%s' resources" % resource.__class__.__name__)

        return meters

    @log_backend_action()
    def get_meter_samples(self, resource, meter_name, start=None, end=None):
        query = [dict(field='resource_id', op='eq', value=resource.backend_id)]

        if start is not None:
            query.append(dict(field='timestamp', op='ge', value=start.strftime('%Y-%m-%dT%H:%M:%S')))
        if end is not None:
            query.append(dict(field='timestamp', op='le', value=end.strftime('%Y-%m-%dT%H:%M:%S')))

        ceilometer = self.ceilometer_client
        try:
            samples = ceilometer.samples.list(meter_name=meter_name, q=query)
        except ceilometer_exceptions.BaseException as e:
            six.reraise(OpenStackBackendError, e)

        return samples
