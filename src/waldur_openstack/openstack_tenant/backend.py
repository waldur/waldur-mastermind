import logging

from cinderclient import exceptions as cinder_exceptions
from cinderclient.v2.contrib import list_extensions
from django.conf import settings
from django.db import transaction
from django.utils import dateparse, timezone
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_core.structure.backend import log_backend_action
from waldur_core.structure.registry import get_resource_type
from waldur_core.structure.signals import resource_pulled
from waldur_core.structure.utils import (
    handle_resource_not_found,
    handle_resource_update_success,
    update_pulled_fields,
)
from waldur_openstack.openstack.models import (
    Flavor,
    FloatingIP,
    Image,
    Port,
    SecurityGroup,
    ServerGroup,
    SubNet,
)
from waldur_openstack.openstack_base.backend import (
    BaseOpenStackBackend,
)
from waldur_openstack.openstack_base.exceptions import OpenStackBackendError
from waldur_openstack.openstack_base.session import (
    get_cinder_client,
    get_keystone_session,
    get_neutron_client,
    get_nova_client,
)

from . import models
from .log import event_logger

logger = logging.getLogger(__name__)


def parse_backend_port(remote_port, **kwargs):
    fixed_ips = remote_port["fixed_ips"]

    local_port = Port(
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


class OpenStackTenantBackend(BaseOpenStackBackend):
    DEFAULTS = {
        "console_type": "novnc",
        "verify_ssl": False,
    }

    def __init__(self, settings):
        super().__init__(settings, settings.options["tenant_id"])
        self.tenant = settings.scope

    @property
    def external_network_id(self):
        return self.settings.options["external_network_id"]

    def pull_service_properties(self):
        self.pull_quotas()
        self.pull_volume_availability_zones()
        self.pull_instance_availability_zones()

    def pull_resources(self):
        self.pull_volumes()
        self.pull_snapshots()
        self.pull_instances()

    def pull_volumes(self):
        backend_volumes = self.get_volumes()
        volumes = models.Volume.objects.filter(
            service_settings=self.settings,
            state__in=[models.Volume.States.OK, models.Volume.States.ERRED],
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
                    volume, backend_volume, models.Volume.get_backend_fields()
                )
                handle_resource_update_success(volume)

    def pull_snapshots(self):
        backend_snapshots = self.get_snapshots()
        snapshots = models.Snapshot.objects.filter(
            service_settings=self.settings,
            state__in=[models.Snapshot.States.OK, models.Snapshot.States.ERRED],
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
                    snapshot, backend_snapshot, models.Snapshot.get_backend_fields()
                )
                handle_resource_update_success(snapshot)

    def pull_instances(self):
        backend_instances = self.get_instances()
        instances = models.Instance.objects.filter(
            service_settings=self.settings,
            state__in=[models.Instance.States.OK, models.Instance.States.ERRED],
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

    def update_instance_fields(self, instance, backend_instance):
        # Preserve flavor fields in Waldur database if flavor is deleted in OpenStack
        fields = set(models.Instance.get_backend_fields())
        flavor_fields = {"flavor_name", "flavor_disk", "ram", "cores", "disk"}
        if not backend_instance.flavor_name:
            fields = fields - flavor_fields
        fields = list(fields)

        update_pulled_fields(instance, backend_instance, fields)

    def _backend_floating_ip_to_floating_ip(self, backend_floating_ip, **kwargs):
        floating_ip = FloatingIP(
            tenant=self.tenant,
            project=self.tenant.project,
            service_settings=self.tenant.service_settings,
            name=backend_floating_ip["floating_ip_address"],
            address=backend_floating_ip["floating_ip_address"],
            backend_network_id=backend_floating_ip["floating_network_id"],
            runtime_state=backend_floating_ip["status"],
            backend_id=backend_floating_ip["id"],
        )
        for field, value in kwargs.items():
            setattr(floating_ip, field, value)

        if "port" not in kwargs:
            floating_ip._port_backend_id = backend_floating_ip["port_id"]

        return floating_ip

    def pull_instance_server_group(self, instance):
        nova = get_nova_client(self.session)
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
                server_group = ServerGroup.objects.get(
                    tenant=self.tenant, backend_id=server_group_backend_id
                )
            except ServerGroup.DoesNotExist:
                logger.exception(
                    f"Server group with id {server_group_backend_id} does not exist in database. "
                    f"Server ID: {server_id}"
                )
            else:
                instance.server_group = server_group

    def pull_quotas(self):
        self._pull_tenant_quotas(self.tenant_id, self.settings)

    @log_backend_action()
    def create_volume(self, volume: models.Volume):
        kwargs = {
            "size": self.mb2gb(volume.size),
            "name": volume.name,
            "description": volume.description,
        }

        if volume.source_snapshot:
            kwargs["snapshot_id"] = volume.source_snapshot.backend_id

        tenant: models.Tenant = volume.service_settings.scope

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
        cinder = get_cinder_client(self.session)
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
    def update_volume(self, volume):
        cinder = get_cinder_client(self.session)
        try:
            cinder.volumes.update(
                volume.backend_id, name=volume.name, description=volume.description
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def delete_volume(self, volume):
        cinder = get_cinder_client(self.session)
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
    def attach_volume(self, volume, instance_uuid, device=None):
        instance = models.Instance.objects.get(uuid=instance_uuid)
        nova = get_nova_client(self.session)
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
    def detach_volume(self, volume):
        nova = get_nova_client(self.session)
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
    def extend_volume(self, volume):
        cinder = get_cinder_client(self.session)
        try:
            cinder.volumes.extend(volume.backend_id, self.mb2gb(volume.size))
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def import_volume(self, backend_id, project=None, save=True):
        """Restore Waldur volume instance based on backend data."""
        cinder = get_cinder_client(self.session)
        try:
            backend_volume = cinder.volumes.get(backend_id)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume = self._backend_volume_to_volume(backend_volume)
        volume.service_settings = self.settings
        volume.project = project
        volume.device = (
            volume.device or ""
        )  # In case if device of an imported volume is null
        if save:
            volume.save()

        return volume

    def _backend_volume_to_volume(self, backend_volume):
        volume_type = None
        availability_zone = None

        try:
            if backend_volume.volume_type:
                volume_type = models.VolumeType.objects.get(
                    name=backend_volume.volume_type,
                    settings=self.tenant.service_settings,
                )
        except models.VolumeType.DoesNotExist:
            pass
        except models.VolumeType.MultipleObjectsReturned:
            logger.error(
                "Volume type is not set as multiple volume types with name %s are found."
                "Service settings UUID: %s",
                (backend_volume.volume_type, self.settings.uuid.hex),
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
            state=models.Volume.States.OK,
            availability_zone=availability_zone,
        )
        if getattr(backend_volume, "volume_image_metadata", False):
            volume.image_metadata = backend_volume.volume_image_metadata
            try:
                image_id = volume.image_metadata.get("image_id")
                if image_id:
                    volume.image = Image.objects.get(
                        settings=self.tenant.service_settings, backend_id=image_id
                    )
            except Image.DoesNotExist:
                pass

            volume.image_name = volume.image_metadata.get("image_name", "")

        # In our setup volume could be attached only to one instance.
        if getattr(backend_volume, "attachments", False):
            if "device" in backend_volume.attachments[0]:
                volume.device = backend_volume.attachments[0]["device"] or ""

            if "server_id" in backend_volume.attachments[0]:
                volume.instance = models.Instance.objects.filter(
                    service_settings=self.settings,
                    backend_id=backend_volume.attachments[0]["server_id"],
                ).first()
        return volume

    def get_volumes(self):
        cinder = get_cinder_client(self.session)
        try:
            backend_volumes = cinder.volumes.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        return [
            self._backend_volume_to_volume(backend_volume)
            for backend_volume in backend_volumes
        ]

    @log_backend_action()
    def remove_bootable_flag(self, volume):
        cinder = get_cinder_client(self.session)
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
            cinder.volumes.set_bootable(backend_volume, False)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.bootable = False
        volume.save(update_fields=["bootable"])

    @log_backend_action()
    def toggle_bootable_flag(self, volume):
        cinder = get_cinder_client(self.session)
        try:
            backend_volume = cinder.volumes.get(volume.backend_id)
            cinder.volumes.set_bootable(backend_volume, volume.bootable)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        volume.save(update_fields=["bootable"])

    @log_backend_action()
    def pull_volume(self, volume, update_fields=None):
        import_time = timezone.now()
        imported_volume = self.import_volume(volume.backend_id, save=False)

        volume.refresh_from_db()
        if volume.modified < import_time:
            if not update_fields:
                update_fields = models.Volume.get_backend_fields()

            update_pulled_fields(volume, imported_volume, update_fields)

        resource_pulled.send(sender=volume.__class__, instance=volume)

    @log_backend_action()
    def pull_volume_runtime_state(self, volume):
        cinder = get_cinder_client(self.session)
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
    def is_volume_deleted(self, volume):
        cinder = get_cinder_client(self.session)
        try:
            cinder.volumes.get(volume.backend_id)
            return False
        except cinder_exceptions.NotFound:
            return True
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def retype_volume(self, volume):
        cinder = get_cinder_client(self.session)
        try:
            cinder.volumes.retype(volume.backend_id, volume.type.name, "on-demand")
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def create_snapshot(self, snapshot, force=True):
        kwargs = {
            "name": snapshot.name,
            "description": snapshot.description,
            "force": force,
        }
        cinder = get_cinder_client(self.session)
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

    def import_snapshot(self, backend_snapshot_id, project=None, save=True):
        """Restore Waldur Snapshot instance based on backend data."""
        cinder = get_cinder_client(self.session)
        try:
            backend_snapshot = cinder.volume_snapshots.get(backend_snapshot_id)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        snapshot = self._backend_snapshot_to_snapshot(backend_snapshot)
        snapshot.service_settings = self.settings
        snapshot.project = project
        if save:
            snapshot.save()
        return snapshot

    def _backend_snapshot_to_snapshot(self, backend_snapshot):
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
                service_settings=self.settings,
                backend_id=backend_snapshot.volume_id,
            ).first()
        return snapshot

    def get_snapshots(self):
        cinder = get_cinder_client(self.session)
        try:
            backend_snapshots = cinder.volume_snapshots.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        return [
            self._backend_snapshot_to_snapshot(backend_snapshot)
            for backend_snapshot in backend_snapshots
        ]

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
        cinder = get_cinder_client(self.session)
        try:
            backend_snapshot = cinder.volume_snapshots.get(snapshot.backend_id)
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        if backend_snapshot.status != snapshot.runtime_state:
            snapshot.runtime_state = backend_snapshot.status
            snapshot.save(update_fields=["runtime_state"])
        return snapshot

    @log_backend_action()
    def delete_snapshot(self, snapshot):
        cinder = get_cinder_client(self.session)
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
    def update_snapshot(self, snapshot):
        cinder = get_cinder_client(self.session)
        try:
            cinder.volume_snapshots.update(
                snapshot.backend_id,
                name=snapshot.name,
                description=snapshot.description,
            )
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action("check is snapshot deleted")
    def is_snapshot_deleted(self, snapshot):
        cinder = get_cinder_client(self.session)
        try:
            cinder.volume_snapshots.get(snapshot.backend_id)
            return False
        except cinder_exceptions.NotFound:
            return True
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def is_volume_availability_zone_supported(self):
        cinder = get_cinder_client(self.session)
        return "AvailabilityZones" in [
            e.name for e in list_extensions.ListExtManager(cinder).show_all()
        ]

    def _create_port_in_external_network(
        self, tenant_uuid, external_network_id, security_groups
    ):
        logger.debug(
            "About to create network port in external network. Network ID: %s.",
            external_network_id,
        )
        neutron = get_neutron_client(self.session)
        try:
            port = {
                "network_id": external_network_id,
                "tenant_id": tenant_uuid,  # admin only functionality
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
        nova = get_nova_client(self.session)

        try:
            backend_flavor = nova.flavors.get(backend_flavor_id)

            # instance key name and fingerprint_md5 are optional
            # it is assumed that if public_key is specified, then
            # key_name and key_fingerprint have valid values
            if public_key:
                backend_public_key = self._get_or_create_ssh_key(
                    instance.key_name, instance.key_fingerprint, public_key
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
                settings.WALDUR_OPENSTACK_TENANT[
                    "ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION"
                ]
                and instance.connect_directly_to_external_network
            ):
                external_network_id = instance.service_settings.options.get(
                    "external_network_id"
                )
                if not external_network_id:
                    raise OpenStackBackendError(
                        "Cannot create an instance directly attached to external network without a defined external_network_id."
                    )
                security_groups = list(
                    instance.security_groups.values_list("backend_id", flat=True)
                )
                external_port_id = self._create_port_in_external_network(
                    instance.service_settings.options["tenant_id"],
                    external_network_id,
                    security_groups,
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
                availability_zone = self.settings.options.get("availability_zone")
                if availability_zone:
                    server_create_parameters["availability_zone"] = availability_zone

            if instance.user_data:
                server_create_parameters["userdata"] = instance.user_data

            if self.settings.options.get("config_drive", False) is True:
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
    def pull_instance_floating_ips(self, instance):
        # method assumes that instance ports are up to date.
        neutron = get_neutron_client(self.session)

        port_mappings = {
            ip.backend_id: ip for ip in instance.ports.all().exclude(backend_id="")
        }
        try:
            backend_floating_ips = neutron.list_floatingips(
                tenant_id=self.tenant_id, port_id=port_mappings.keys()
            )["floatingips"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

        backend_ids = {fip["id"] for fip in backend_floating_ips}

        floating_ips = {
            fip.backend_id: fip
            for fip in FloatingIP.objects.filter(
                tenant=self.tenant,
                backend_id__in=backend_ids,
            )
        }

        with transaction.atomic():
            for backend_floating_ip in backend_floating_ips:
                imported_floating_ip = self._backend_floating_ip_to_floating_ip(
                    backend_floating_ip
                )
                port = port_mappings.get(imported_floating_ip._port_backend_id)

                floating_ip = floating_ips.get(imported_floating_ip.backend_id)
                if floating_ip is None:
                    imported_floating_ip.port = port
                    imported_floating_ip.save()
                    continue
                elif floating_ip.state == FloatingIP.States.OK:
                    continue

                # Don't update user defined name.
                if floating_ip.address != floating_ip.name:
                    imported_floating_ip.name = floating_ip.name
                update_pulled_fields(
                    floating_ip,
                    imported_floating_ip,
                    FloatingIP.get_backend_fields(),
                )

                if floating_ip.port != port:
                    floating_ip.port = port
                    floating_ip.save()

            frontend_ids = set(
                instance.floating_ips.filter(state=FloatingIP.States.OK)
                .exclude(backend_id="")
                .values_list("backend_id", flat=True)
            )
            stale_ids = frontend_ids - backend_ids
            logger.info("About to detach floating IPs from ports: %s", stale_ids)
            instance.floating_ips.filter(backend_id__in=stale_ids).update(port=None)

    @log_backend_action()
    def push_instance_floating_ips(self, instance):
        neutron = get_neutron_client(self.session)
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
                    floating_ip = FloatingIP(
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

    def _get_or_create_ssh_key(self, key_name, fingerprint_md5, public_key):
        nova = get_nova_client(self.session)

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
    def update_instance(self, instance):
        nova = get_nova_client(self.session)
        try:
            nova.servers.update(
                instance.backend_id,
                name=instance.name,
                description=instance.description,
            )
        except keystone_exceptions.NotFound as e:
            raise OpenStackBackendError(e)

    def get_admin_session(self):
        return get_keystone_session(self.settings.scope.service_settings)

    def import_instance(
        self, backend_id, project=None, save=True, connected_internal_network_names=None
    ):
        # NB! This method does not import instance sub-objects like security groups or ports.
        #     They have to be pulled separately.

        if connected_internal_network_names is None:
            connected_internal_network_names = set()

        if self.settings.scope:
            # We need to use admin client from shared service settings to get instance hypervisor_hostname.
            session = self.get_admin_session()
        else:
            session = self.session

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
            backend_instance, flavor_id, connected_internal_network_names
        )
        with transaction.atomic():
            instance.service_settings = self.settings
            instance.project = project
            if hasattr(backend_instance, "fault"):
                instance.error_message = backend_instance.fault["message"]
            if save:
                instance.save()
                volumes = self._import_instance_volumes(
                    attached_volume_ids, project, save
                )
                instance.volumes.add(*volumes)

        return instance

    def _import_instance_volumes(self, attached_volume_ids, project, save):
        # import instance volumes, or use existed if they already exist in Waldur.
        volumes = []
        for backend_volume_id in attached_volume_ids:
            try:
                volumes.append(
                    models.Volume.objects.get(
                        service_settings=self.settings,
                        backend_id=backend_volume_id,
                    )
                )
            except models.Volume.DoesNotExist:
                volumes.append(self.import_volume(backend_volume_id, project, save))
        return volumes

    def _backend_instance_to_instance(
        self,
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
                    name=availability_zone_name, settings=self.settings
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
                flavor = Flavor.objects.get(
                    settings=self.tenant.service_settings, backend_id=backend_flavor_id
                )
                instance.flavor_name = flavor.name
                instance.flavor_disk = flavor.disk
                instance.cores = flavor.cores
                instance.ram = flavor.ram
            except Flavor.DoesNotExist:
                backend_flavor = self._get_flavor(backend_flavor_id)
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
            attached_volume_ids, project=None, save=False
        )
        instance.disk = sum(volume.size for volume in volumes)

        return instance

    def _get_flavor(self, flavor_id):
        nova = get_nova_client(self.session)
        try:
            return nova.flavors.get(flavor_id)
        except nova_exceptions.NotFound:
            logger.info("OpenStack flavor %s is gone.", flavor_id)
            return None
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def get_instances(self):
        if self.settings.scope:
            # We need to use admin client from shared service settings to get instance hypervisor_hostname.
            session = self.get_admin_session()
        else:
            session = self.session

        nova = get_nova_client(session)

        try:
            # We use search_opts according to the rules in
            # https://docs.openstack.org/api-ref/compute/?expanded=list-servers-detail#list-server-request
            backend_instances = nova.servers.list(
                search_opts={"project_id": self.tenant_id, "all_tenants": 1}
            )
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        instances = []
        for backend_instance in backend_instances:
            flavor_id = backend_instance.flavor["id"]
            instances.append(
                self._backend_instance_to_instance(backend_instance, flavor_id)
            )
        return instances

    def get_importable_instances(self):
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
            for instance in self.get_instances()
        ]
        return self.get_importable_resources(models.Instance, instances)

    def get_expired_instances(self):
        instances = [instance.backend_id for instance in self.get_instances()]
        return self.get_expired_resources(models.Instance, instances)

    def get_expired_volumes(self):
        volumes = [volumes.backend_id for volumes in self.get_volumes()]
        return self.get_expired_resources(models.Volume, volumes)

    def get_importable_volumes(self):
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
            for volume in self.get_volumes()
        ]
        return self.get_importable_resources(models.Volume, volumes)

    @transaction.atomic()
    def _pull_zones(self, backend_zones, frontend_model, default_zone="nova"):
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
            frontend_model.objects.create(
                settings=self.settings,
                name=zone,
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

    def pull_instance_availability_zones(self):
        nova = get_nova_client(self.session)
        try:
            # By default detailed flag is True, but OpenStack policy for detailed data is disabled.
            # Therefore we should explicitly pass detailed=False. Otherwise request fails.
            backend_zones = nova.availability_zones.list(detailed=False)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        self._pull_zones(backend_zones, models.InstanceAvailabilityZone)

    @log_backend_action()
    def pull_instance(self, instance: models.Instance, update_fields=None):
        import_time = timezone.now()
        connected_internal_network_names = set(
            instance.ports.all().values_list("subnet__network__name", flat=True)
        )
        imported_instance = self.import_instance(
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
        neutron = get_neutron_client(self.session)
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
            for ip in (Port.objects.filter(tenant=self.tenant).exclude(backend_id=""))
        }

        subnets = SubNet.objects.filter(tenant=self.tenant)
        subnet_mappings = {subnet.backend_id: subnet for subnet in subnets}

        with transaction.atomic():
            for backend_port in backend_ports:
                imported_port = parse_backend_port(backend_port, instance=instance)
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
                        Port.get_backend_fields() + ("backend_id",),
                    )

                elif imported_port.backend_id in existing_ips:
                    port = existing_ips[imported_port.backend_id]
                    update_pulled_fields(
                        port,
                        imported_port,
                        Port.get_backend_fields(),
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
    def push_instance_ports(self, instance):
        # we assume that port subnet cannot be changed
        neutron = get_neutron_client(self.session)
        nova = get_nova_client(self.session)

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
    def create_instance_ports(self, instance):
        security_groups = list(
            instance.security_groups.values_list("backend_id", flat=True)
        )
        for port in instance.ports.all():
            self.create_port(port, security_groups)

    def create_port(self, port: models.Port, security_groups):
        neutron = get_neutron_client(self.session)

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
    def delete_instance_ports(self, instance):
        for port in instance.ports.all():
            if port.backend_id:
                self.delete_port(port)

    def delete_port(self, port):
        neutron = get_neutron_client(self.session)

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
        self, instance, backend_id, allowed_address_pairs
    ):
        neutron = get_neutron_client(self.session)
        try:
            neutron.update_port(
                backend_id, {"port": {"allowed_address_pairs": allowed_address_pairs}}
            )
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def pull_instance_security_groups(self, instance):
        nova = get_nova_client(self.session)
        server_id = instance.backend_id
        try:
            remote_groups = nova.servers.list_security_group(server_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)
        tenant_groups = SecurityGroup.objects.filter(tenant=self.tenant)

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
            except SecurityGroup.DoesNotExist:
                logger.exception(
                    f"Security group with id {group_id} does not exist in database. "
                    f"Server ID: {server_id}"
                )
            else:
                instance.security_groups.add(security_group)

    @log_backend_action()
    def push_instance_security_groups(self, instance):
        nova = get_nova_client(self.session)
        server_id = instance.backend_id
        try:
            backend_ids = set(g.id for g in nova.servers.list_security_group(server_id))
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        nc_ids = set(
            SecurityGroup.objects.filter(instances=instance)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )

        # remove stale groups
        for group_id in backend_ids - nc_ids:
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
        for group_id in nc_ids - backend_ids:
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
    def delete_instance(self, instance):
        nova = get_nova_client(self.session)
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
    def is_instance_deleted(self, instance):
        nova = get_nova_client(self.session)
        try:
            nova.servers.get(instance.backend_id)
            return False
        except nova_exceptions.NotFound:
            return True
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def start_instance(self, instance):
        nova = get_nova_client(self.session)
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
    def stop_instance(self, instance):
        nova = get_nova_client(self.session)
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
    def restart_instance(self, instance):
        nova = get_nova_client(self.session)
        try:
            nova.servers.reboot(instance.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def resize_instance(self, instance, flavor_id):
        nova = get_nova_client(self.session)
        try:
            nova.servers.resize(instance.backend_id, flavor_id, "MANUAL")
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def pull_instance_runtime_state(self, instance):
        nova = get_nova_client(self.session)
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
    def confirm_instance_resize(self, instance):
        nova = get_nova_client(self.session)
        try:
            nova.servers.confirm_resize(instance.backend_id)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    @log_backend_action()
    def get_console_url(self, instance):
        nova = get_nova_client(self.session)
        url = None
        console_type = self.settings.get_option("console_type")
        try:
            url = nova.servers.get_console_url(instance.backend_id, console_type)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        # newer API seems to return remote_console sometimes. According to spec it should be 'console'
        if "console" in url:
            return url["console"]["url"]
        elif "remote_console" in url:
            return url["remote_console"]["url"]

    @log_backend_action()
    def get_console_output(self, instance, length=None):
        nova = get_nova_client(self.session)
        try:
            return nova.servers.get_console_output(instance.backend_id, length)
        except nova_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

    def pull_volume_availability_zones(self):
        if not self.is_volume_availability_zone_supported():
            return

        try:
            cinder = get_cinder_client(self.session)
            backend_zones = cinder.availability_zones.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        self._pull_zones(backend_zones, models.VolumeAvailabilityZone)
