from django.conf import settings
from django.db.models import OuterRef, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from keystoneauth1.exceptions.connection import ConnectFailure
from rest_framework import decorators, exceptions, generics, response, status
from rest_framework import serializers as rf_serializers

import waldur_openstack.openstack.serializers
from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import signals as structure_signals
from waldur_core.structure import views as structure_views
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.signals import resource_imported
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack import views as openstack_views
from waldur_openstack.openstack.apps import OpenStackConfig
from waldur_openstack.openstack_base.exceptions import OpenStackBackendError
from waldur_openstack.openstack_tenant import backend as openstack_tenant_backend

from . import executors, filters, models, serializers


class VolumeViewSet(structure_views.ResourceViewSet):
    queryset = models.Volume.objects.all().order_by("name")
    serializer_class = serializers.VolumeSerializer
    filterset_class = filters.VolumeFilter

    update_executor = executors.VolumeUpdateExecutor
    pull_executor = executors.VolumePullExecutor

    def create(self, request, *args, **kwargs):
        return response.Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        return response.Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _is_volume_bootable(volume):
        if volume.bootable:
            raise core_exceptions.IncorrectStateException(
                _("Volume cannot be bootable.")
            )

    def _is_volume_attached(volume):
        if not volume.instance:
            raise core_exceptions.IncorrectStateException(
                _("Volume is not attached to an instance.")
            )

    def _is_volume_instance_shutoff(volume):
        if (
            volume.instance
            and volume.instance.runtime_state != models.Instance.RuntimeStates.SHUTOFF
        ):
            raise core_exceptions.IncorrectStateException(
                _("Volume instance should be in shutoff state.")
            )

    def _is_volume_instance_ok(volume):
        if volume.instance and volume.instance.state != models.Instance.States.OK:
            raise core_exceptions.IncorrectStateException(
                _("Volume instance should be in OK state.")
            )

    @decorators.action(detail=True, methods=["post"])
    def extend(self, request, uuid=None):
        """Increase volume size"""
        volume = self.get_object()
        old_size = volume.size
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        volume.refresh_from_db()
        executors.VolumeExtendExecutor().execute(
            volume, old_size=old_size, new_size=volume.size
        )

        return response.Response(
            {"status": _("extend was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    extend_validators = [
        _is_volume_bootable,
        _is_volume_instance_ok,
        _is_volume_instance_shutoff,
        core_validators.StateValidator(models.Volume.States.OK),
    ]
    extend_serializer_class = serializers.VolumeExtendSerializer

    @decorators.action(detail=True, methods=["post"])
    def snapshot(self, request, uuid=None):
        """Create snapshot from volume"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        snapshot = serializer.save()

        executors.SnapshotCreateExecutor().execute(snapshot)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    snapshot_serializer_class = serializers.SnapshotSerializer

    @decorators.action(detail=True, methods=["post"])
    def create_snapshot_schedule(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_snapshot_schedule_validators = [
        core_validators.StateValidator(models.Volume.States.OK)
    ]
    create_snapshot_schedule_serializer_class = serializers.SnapshotScheduleSerializer

    @decorators.action(detail=True, methods=["post"])
    def attach(self, request, uuid=None):
        """Attach volume to instance"""
        volume = self.get_object()
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.VolumeAttachExecutor().execute(volume)
        return response.Response(
            {"status": _("attach was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    attach_validators = [
        core_validators.RuntimeStateValidator("available"),
        core_validators.StateValidator(models.Volume.States.OK),
    ]
    attach_serializer_class = serializers.VolumeAttachSerializer

    @decorators.action(detail=True, methods=["post"])
    def detach(self, request, uuid=None):
        """Detach instance from volume"""
        volume = self.get_object()
        executors.VolumeDetachExecutor().execute(volume)
        return response.Response(
            {"status": _("detach was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    detach_validators = [
        _is_volume_bootable,
        _is_volume_attached,
        core_validators.RuntimeStateValidator("in-use"),
        core_validators.StateValidator(models.Volume.States.OK),
    ]

    @decorators.action(detail=True, methods=["post"])
    def retype(self, request, uuid=None):
        """Retype detached volume"""
        volume = self.get_object()
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.VolumeRetypeExecutor().execute(volume)
        return response.Response(
            {"status": _("retype was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    retype_validators = [
        core_validators.RuntimeStateValidator("available"),
        core_validators.StateValidator(models.Volume.States.OK),
    ]

    retype_serializer_class = serializers.VolumeRetypeSerializer

    @decorators.action(detail=True, methods=["get"])
    def counters(self, request, uuid=None):
        instance = self.get_object()
        return response.Response(
            {
                "snapshots": instance.snapshots.count(),
                "snapshot_schedules": instance.snapshot_schedules.count(),
            }
        )


class SnapshotViewSet(structure_views.ResourceViewSet):
    queryset = models.Snapshot.objects.all().order_by("name")
    serializer_class = serializers.SnapshotSerializer
    update_executor = executors.SnapshotUpdateExecutor
    delete_executor = executors.SnapshotDeleteExecutor
    pull_executor = executors.SnapshotPullExecutor
    filterset_class = filters.SnapshotFilter
    disabled_actions = ["create"]

    @decorators.action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        restoration = serializer.save()

        executors.SnapshotRestorationExecutor().execute(restoration)
        serialized_volume = serializers.VolumeSerializer(
            restoration.volume, context={"request": self.request}
        )
        resource_imported.send(
            sender=models.Volume,
            instance=restoration.volume,
        )
        return response.Response(serialized_volume.data, status=status.HTTP_201_CREATED)

    restore_serializer_class = serializers.SnapshotRestorationSerializer
    restore_validators = [core_validators.StateValidator(models.Snapshot.States.OK)]

    @decorators.action(detail=True, methods=["get"])
    def restorations(self, request, uuid=None):
        snapshot = self.get_object()
        serializer = self.get_serializer(snapshot.restorations.all(), many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    restorations_serializer_class = serializers.SnapshotRestorationSerializer


class InstanceAvailabilityZoneViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.InstanceAvailabilityZone.objects.all().order_by(
        "settings", "name"
    )
    serializer_class = serializers.InstanceAvailabilityZoneSerializer
    lookup_field = "uuid"
    filterset_class = filters.InstanceAvailabilityZoneFilter


class InstanceViewSet(structure_views.ResourceViewSet):
    """
    OpenStack instance permissions
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

    - Staff members can list all available VM instances in any service.
    - Customer owners can list all VM instances in all the services that belong to any of the customers they own.
    - Project administrators can list all VM instances, create new instances and start/stop/restart instances in all the
      services that are connected to any of the projects they are administrators in.
    - Project managers can list all VM instances in all the services that are connected to any of the projects they are
      managers in.
    """

    queryset = models.Instance.objects.all()
    serializer_class = serializers.InstanceSerializer
    filterset_class = filters.InstanceFilter
    filter_backends = structure_views.ResourceViewSet.filter_backends + (
        structure_filters.StartTimeFilter,
    )
    pull_executor = executors.InstancePullExecutor
    pull_serializer_class = rf_serializers.Serializer

    update_executor = executors.InstanceUpdateExecutor
    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Instance.States.OK)
    ]

    def create(self, request, *args, **kwargs):
        return response.Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        return response.Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _has_backups(instance):
        if instance.backups.exists():
            raise core_exceptions.IncorrectStateException(
                _("Cannot delete instance that has backups.")
            )

    def _has_snapshots(instance):
        for volume in instance.volumes.all():
            if volume.snapshots.exists():
                raise core_exceptions.IncorrectStateException(
                    _("Cannot delete instance that has snapshots.")
                )

    def _can_destroy_instance(instance):
        if instance.state == models.Instance.States.ERRED:
            return
        if (
            instance.state == models.Instance.States.OK
            and instance.runtime_state == models.Instance.RuntimeStates.SHUTOFF
        ):
            return
        if (
            instance.state == models.Instance.States.OK
            and instance.runtime_state == models.Instance.RuntimeStates.ACTIVE
        ):
            raise core_exceptions.IncorrectStateException(
                _("Please stop the instance before its removal.")
            )
        raise core_exceptions.IncorrectStateException(
            _("Instance should be shutoff and OK or erred. " "Please contact support.")
        )

    @decorators.action(detail=True, methods=["post"])
    def change_flavor(self, request, uuid=None):
        instance = self.get_object()
        old_flavor_name = instance.flavor_name
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        flavor = serializer.validated_data.get("flavor")
        executors.InstanceFlavorChangeExecutor().execute(
            instance, flavor=flavor, old_flavor_name=old_flavor_name
        )
        return response.Response(
            {"status": _("change_flavor was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    def _can_change_flavor(instance):
        if (
            instance.state == models.Instance.States.OK
            and instance.runtime_state == models.Instance.RuntimeStates.ACTIVE
        ):
            raise core_exceptions.IncorrectStateException(
                _("Please stop the instance before changing its flavor.")
            )

    change_flavor_serializer_class = serializers.InstanceFlavorChangeSerializer
    change_flavor_validators = [
        _can_change_flavor,
        core_validators.StateValidator(models.Instance.States.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.SHUTOFF),
    ]

    @decorators.action(detail=True, methods=["post"])
    def start(self, request, uuid=None):
        instance = self.get_object()
        executors.InstanceStartExecutor().execute(instance)
        return response.Response(
            {"status": _("start was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _can_start_instance(instance):
        if (
            instance.state == models.Instance.States.OK
            and instance.runtime_state == models.Instance.RuntimeStates.ACTIVE
        ):
            raise core_exceptions.IncorrectStateException(
                _("Instance is already active.")
            )

    start_validators = [
        _can_start_instance,
        core_validators.StateValidator(models.Instance.States.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.SHUTOFF),
    ]
    start_serializer_class = rf_serializers.Serializer

    @decorators.action(detail=True, methods=["post"])
    def stop(self, request, uuid=None):
        instance = self.get_object()
        executors.InstanceStopExecutor().execute(instance)
        return response.Response(
            {"status": _("stop was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _can_stop_instance(instance):
        if (
            instance.state == models.Instance.States.OK
            and instance.runtime_state == models.Instance.RuntimeStates.SHUTOFF
        ):
            raise core_exceptions.IncorrectStateException(
                _("Instance is already stopped.")
            )

    stop_validators = [
        _can_stop_instance,
        core_validators.StateValidator(models.Instance.States.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.ACTIVE),
    ]
    stop_serializer_class = rf_serializers.Serializer

    @decorators.action(detail=True, methods=["post"])
    def restart(self, request, uuid=None):
        instance = self.get_object()
        executors.InstanceRestartExecutor().execute(instance)
        return response.Response(
            {"status": _("restart was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _can_restart_instance(instance):
        if (
            instance.state == models.Instance.States.OK
            and instance.runtime_state == models.Instance.RuntimeStates.SHUTOFF
        ):
            raise core_exceptions.IncorrectStateException(
                _("Please start instance first.")
            )

    restart_validators = [
        _can_restart_instance,
        core_validators.StateValidator(models.Instance.States.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.ACTIVE),
    ]
    restart_serializer_class = rf_serializers.Serializer

    @decorators.action(detail=True, methods=["post"])
    def update_security_groups(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.InstanceUpdateSecurityGroupsExecutor().execute(instance)
        return response.Response(
            {"status": _("security groups update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_security_groups_validators = [
        core_validators.StateValidator(models.Instance.States.OK)
    ]
    update_security_groups_serializer_class = (
        serializers.InstanceSecurityGroupsUpdateSerializer
    )

    @decorators.action(detail=True, methods=["post"])
    def backup(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        backup = serializer.save()

        executors.BackupCreateExecutor().execute(backup)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    backup_validators = [core_validators.StateValidator(models.Instance.States.OK)]
    backup_serializer_class = serializers.BackupSerializer

    @decorators.action(detail=True, methods=["post"])
    def create_backup_schedule(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_backup_schedule_validators = [
        core_validators.StateValidator(models.Instance.States.OK)
    ]
    create_backup_schedule_serializer_class = serializers.BackupScheduleSerializer

    @decorators.action(detail=True, methods=["post"])
    def update_allowed_address_pairs(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        subnet = serializer.validated_data["subnet"]
        allowed_address_pairs = serializer.validated_data["allowed_address_pairs"]
        try:
            port = openstack_models.Port.objects.get(instance=instance, subnet=subnet)
        except openstack_models.Port.DoesNotExist:
            return response.Response(
                {"status": _("Port is not found.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except openstack_models.Port.MultipleObjectsReturned:
            return response.Response(
                {"status": _("Multiple ports are found.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        executors.InstanceAllowedAddressPairsUpdateExecutor().execute(
            instance,
            backend_id=port.backend_id,
            allowed_address_pairs=allowed_address_pairs,
        )
        return response.Response(
            {"status": _("Allowed address pairs update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_allowed_address_pairs_validators = [
        core_validators.StateValidator(models.Instance.States.OK)
    ]
    update_allowed_address_pairs_serializer_class = (
        serializers.InstanceAllowedAddressPairsUpdateSerializer
    )

    @decorators.action(detail=True, methods=["post"])
    def update_ports(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.InstancePortsUpdateExecutor().execute(instance)
        return response.Response(
            {"status": _("internal ips update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_ports_validators = [
        core_validators.StateValidator(models.Instance.States.OK)
    ]
    update_ports_serializer_class = serializers.InstancePortsUpdateSerializer

    @decorators.action(detail=True, methods=["get"])
    def ports(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance.ports.all(), many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    ports_serializer_class = waldur_openstack.openstack.serializers.NestedPortSerializer

    @decorators.action(detail=True, methods=["post"])
    def update_floating_ips(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.InstanceFloatingIPsUpdateExecutor().execute(instance)
        return response.Response(
            {"status": _("Floating IPs update was scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_floating_ips_validators = [
        core_validators.StateValidator(models.Instance.States.OK)
    ]
    update_floating_ips_serializer_class = (
        serializers.InstanceFloatingIPsUpdateSerializer
    )

    @decorators.action(detail=True, methods=["get"])
    def floating_ips(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(
            instance=instance.floating_ips.all(),
            queryset=openstack_models.FloatingIP.objects.all(),
            many=True,
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    floating_ips_serializer_class = (
        waldur_openstack.openstack.serializers.NestedFloatingIPSerializer
    )

    @decorators.action(detail=True, methods=["get"])
    def console(self, request, uuid=None):
        instance = self.get_object()
        backend = instance.get_backend()
        try:
            url = backend.get_console_url(instance)
        except OpenStackBackendError as e:
            raise exceptions.ValidationError(str(e))

        return response.Response({"url": url}, status=status.HTTP_200_OK)

    console_validators = [core_validators.StateValidator(models.Instance.States.OK)]

    def check_permissions_for_console(request, view, instance=None):
        if not instance:
            return

        if request.user.is_staff:
            return

        if settings.WALDUR_OPENSTACK_TENANT[
            "ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS"
        ]:
            structure_permissions.is_administrator(request, view, instance)
        else:
            raise exceptions.PermissionDenied()

    console_permissions = [check_permissions_for_console]

    @decorators.action(detail=True, methods=["get"])
    def console_log(self, request, uuid=None):
        instance = self.get_object()
        backend = instance.get_backend()
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        length = serializer.validated_data.get("length")

        try:
            log = backend.get_console_output(instance, length)
        except OpenStackBackendError as e:
            raise exceptions.ValidationError(str(e))

        return response.Response(log, status=status.HTTP_200_OK)

    console_log_serializer_class = serializers.ConsoleLogSerializer
    console_log_permissions = [structure_permissions.is_administrator]

    @decorators.action(detail=True, methods=["get"])
    def counters(self, request, uuid=None):
        instance = self.get_object()
        return response.Response(
            {
                "volumes": instance.volumes.count(),
                "backups": instance.backups.count(),
                "backup_schedules": instance.backup_schedules.count(),
                "security_groups": instance.security_groups.count(),
                "internal_ips": instance.ports.count(),
                "floating_ips": instance.floating_ips.count(),
            }
        )


class MarketplaceInstanceViewSet(structure_views.ResourceViewSet):
    queryset = models.Instance.objects.all()
    serializer_class = serializers.InstanceSerializer
    filter_backends = structure_views.ResourceViewSet.filter_backends + (
        structure_filters.StartTimeFilter,
    )

    def destroy(self, request, uuid=None):
        """
        Deletion of an instance is done through sending a **DELETE** request to the instance URI.
        Valid request example (token is user specific):

        .. code-block:: http

            DELETE /api/openstacktenant-instances/abceed63b8e844afacd63daeac855474/ HTTP/1.1
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

        Only stopped instances or instances in ERRED state can be deleted.

        By default when instance is destroyed, all data volumes
        attached to it are destroyed too. In order to preserve data
        volumes use query parameter ?delete_volumes=false
        In this case data volumes are detached from the instance and
        then instance is destroyed. Note that system volume is deleted anyway.
        For example:

        .. code-block:: http

            DELETE /api/openstacktenant-instances/abceed63b8e844afacd63daeac855474/?delete_volumes=false HTTP/1.1
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

        """
        serializer = self.get_serializer(
            data=request.query_params, instance=self.get_object()
        )
        serializer.is_valid(raise_exception=True)
        delete_volumes = serializer.validated_data["delete_volumes"]
        release_floating_ips = serializer.validated_data["release_floating_ips"]

        resource = self.get_object()
        force = resource.state == models.Instance.States.ERRED
        executors.InstanceDeleteExecutor.execute(
            resource,
            force=force,
            delete_volumes=delete_volumes,
            release_floating_ips=release_floating_ips,
            is_async=self.async_executor,
        )

        return response.Response(
            {"status": _("destroy was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    destroy_validators = [
        InstanceViewSet._can_destroy_instance,
        InstanceViewSet._has_backups,
        InstanceViewSet._has_snapshots,
    ]
    destroy_serializer_class = serializers.InstanceDeleteSerializer

    @decorators.action(detail=True, methods=["delete"])
    def force_destroy(self, request, uuid=None):
        """This action completely repeats 'destroy', with the exclusion of validators.
        Destroy's validators require stopped VM. This requirement has expired.
        But for compatibility with old documentation, it must be left.
        """
        return self.destroy(request, uuid)

    force_destroy_validators = [
        InstanceViewSet._has_backups,
        InstanceViewSet._has_snapshots,
        core_validators.StateValidator(
            models.Instance.States.OK,
            models.Instance.States.ERRED,
        ),
    ]
    force_destroy_serializer_class = destroy_serializer_class

    def perform_create(self, serializer):
        instance = serializer.save()
        executors.InstanceCreateExecutor.execute(
            instance,
            ssh_key=serializer.validated_data.get("ssh_public_key"),
            flavor=serializer.validated_data["flavor"],
            server_group=serializer.validated_data.get("server_group"),
            is_heavy_task=True,
        )


class MarketplaceVolumeViewSet(structure_views.ResourceViewSet):
    queryset = models.Volume.objects.all().order_by("name")
    serializer_class = serializers.VolumeSerializer
    filterset_class = filters.VolumeFilter

    create_executor = executors.VolumeCreateExecutor

    def _can_destroy_volume(volume):
        if volume.state == models.Volume.States.ERRED:
            return
        if volume.state != models.Volume.States.OK:
            raise core_exceptions.IncorrectStateException(
                _("Volume should be in OK state.")
            )
        core_validators.RuntimeStateValidator(
            "available", "error", "error_restoring", "error_extending", ""
        )(volume)

    def _volume_snapshots_exist(volume):
        if volume.snapshots.exists():
            raise core_exceptions.IncorrectStateException(
                _("Volume has dependent snapshots.")
            )

    delete_executor = executors.VolumeDeleteExecutor
    destroy_validators = [
        _can_destroy_volume,
        _volume_snapshots_exist,
    ]


class BackupViewSet(structure_views.ResourceViewSet):
    queryset = models.Backup.objects.all().order_by("name")
    serializer_class = serializers.BackupSerializer
    filterset_class = filters.BackupFilter
    disabled_actions = ["create"]

    delete_executor = executors.BackupDeleteExecutor

    # method has to be overridden in order to avoid triggering of UpdateExecutor
    # which is a default action for all ResourceViewSet(s)
    def perform_update(self, serializer):
        serializer.save()

    @decorators.action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        backup_restoration = serializer.save()

        # Note that connected volumes will be linked with new marketplace.Resources by handler in openstack_marketplace
        structure_signals.resource_imported.send(
            sender=models.Instance,
            instance=backup_restoration.instance,
        )

        # It is assumed that SSH public key is already stored in OpenStack system volume.
        # Therefore we don't need to specify it explicitly for cloud init service.
        executors.InstanceCreateExecutor.execute(
            backup_restoration.instance,
            flavor=backup_restoration.flavor,
            is_heavy_task=True,
        )

        instance_serializer = serializers.InstanceSerializer(
            backup_restoration.instance, context={"request": self.request}
        )
        return response.Response(
            instance_serializer.data, status=status.HTTP_201_CREATED
        )

    restore_validators = [core_validators.StateValidator(models.Backup.States.OK)]
    restore_serializer_class = serializers.BackupRestorationSerializer


class BaseScheduleViewSet(structure_views.ResourceViewSet):
    disabled_actions = ["create"]

    # method has to be overridden in order to avoid triggering of UpdateExecutor
    # which is a default action for all ResourceViewSet(s)
    def perform_update(self, serializer):
        serializer.save()

    # method has to be overridden in order to avoid triggering of DeleteExecutor
    # which is a default action for all ResourceViewSet(s)
    def destroy(self, request, *args, **kwargs):
        resource = self.get_object()
        resource.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    def list(self, request, *args, **kwargs):
        """
        For schedule to work, it should be activated - it's flag is_active set to true. If it's not, it won't be used
        for triggering the next operations. Schedule will be deactivated if operation fails.

        - **retention time** is a duration in days during which resource is preserved.
        - **maximal_number_of_resources** is a maximal number of active resources connected to this schedule.
        - **schedule** is a resource schedule defined in a cron format.
        - **timezone** is used for calculating next run of the resource schedule (optional).

        A schedule can be it two states: active or not. Non-active states are not used for scheduling the new tasks.
        Only users with write access to schedule resource can activate or deactivate a schedule.
        """
        return super().list(self, request, *args, **kwargs)

    def _is_schedule_active(resource_schedule):
        if resource_schedule.is_active:
            raise core_exceptions.IncorrectStateException(
                _("Resource schedule is already activated.")
            )

    @decorators.action(detail=True, methods=["post"])
    def activate(self, request, uuid):
        """
        Activate a resource schedule. Note that
        if a schedule is already active, this will result in **409 CONFLICT** code.
        """
        schedule = self.get_object()
        schedule.is_active = True
        schedule.error_message = ""
        schedule.save()
        return response.Response({"status": _("A schedule was activated")})

    activate_validators = [_is_schedule_active]

    def _is_schedule_deactivated(resource_schedule):
        if not resource_schedule.is_active:
            raise core_exceptions.IncorrectStateException(
                _("A schedule is already deactivated.")
            )

    @decorators.action(detail=True, methods=["post"])
    def deactivate(self, request, uuid):
        """
        Deactivate a resource schedule. Note that
        if a schedule was already deactivated, this will result in **409 CONFLICT** code.
        """
        schedule = self.get_object()
        schedule.is_active = False
        schedule.save()
        return response.Response({"status": _("Backup schedule was deactivated")})

    deactivate_validators = [_is_schedule_deactivated]


class BackupScheduleViewSet(BaseScheduleViewSet):
    queryset = models.BackupSchedule.objects.all().order_by("name")
    serializer_class = serializers.BackupScheduleSerializer
    filterset_class = filters.BackupScheduleFilter


class SnapshotScheduleViewSet(BaseScheduleViewSet):
    queryset = models.SnapshotSchedule.objects.all().order_by("name")
    serializer_class = serializers.SnapshotScheduleSerializer
    filterset_class = filters.SnapshotScheduleFilter


class SharedSettingsBaseView(generics.GenericAPIView):
    def get_private_settings(self):
        service_settings_uuid = self.request.query_params.get("service_settings_uuid")
        if not service_settings_uuid or not core_utils.is_uuid_like(
            service_settings_uuid
        ):
            return structure_models.ServiceSettings.objects.none()

        queryset = structure_models.ServiceSettings.objects.filter(
            type=OpenStackConfig.service_name
        )
        queryset = filter_queryset_for_user(queryset, self.request.user)
        try:
            shared_settings = queryset.get(uuid=service_settings_uuid)
        except structure_models.ServiceSettings.DoesNotExist:
            return structure_models.ServiceSettings.objects.none()

        tenants = openstack_models.Tenant.objects.filter(
            service_settings=shared_settings
        )
        tenants = filter_queryset_for_user(tenants, self.request.user)
        if tenants:
            return structure_models.ServiceSettings.objects.filter(scope__in=tenants)
        else:
            return structure_models.ServiceSettings.objects.none()

    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)


class SharedSettingsInstances(SharedSettingsBaseView):
    serializer_class = serializers.InstanceSerializer

    def get_queryset(self):
        private_settings = self.get_private_settings()
        return models.Instance.objects.order_by("project__customer__name").filter(
            service_settings__in=private_settings
        )


class SharedSettingsCustomers(SharedSettingsBaseView):
    serializer_class = serializers.SharedSettingsCustomerSerializer

    def get_queryset(self):
        private_settings = self.get_private_settings()
        vms = models.Instance.objects.filter(
            service_settings__in=private_settings,
            project__customer=OuterRef("pk"),
        )
        vm_count = Coalesce(core_utils.SubqueryCount(vms), Value(0))
        return structure_models.Customer.objects.filter(
            pk__in=private_settings.values("customer")
        ).annotate(vm_count=vm_count)


class VolumeAvailabilityZoneViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.VolumeAvailabilityZone.objects.all().order_by("settings", "name")
    serializer_class = serializers.VolumeAvailabilityZoneSerializer
    lookup_field = "uuid"
    filterset_class = filters.VolumeAvailabilityZoneFilter


def backend_instances(self, request, uuid=None):
    tenant = self.get_object()
    service_settings = get_object_or_404(
        structure_models.PrivateServiceSettings.objects, scope=tenant
    )
    backend = openstack_tenant_backend.OpenStackTenantBackend(service_settings)
    try:
        serializer = serializers.BackendInstanceSerializer(
            backend.get_instances(), many=True
        )
    except (ConnectFailure, OpenStackBackendError) as e:
        raise exceptions.ValidationError(e)
    return response.Response(serializer.data, status=status.HTTP_200_OK)


openstack_views.TenantViewSet.backend_instances = decorators.action(detail=True)(
    backend_instances
)


def backend_volumes(self, request, uuid=None):
    tenant = self.get_object()
    service_settings = get_object_or_404(
        structure_models.PrivateServiceSettings.objects, scope=tenant
    )
    backend = openstack_tenant_backend.OpenStackTenantBackend(service_settings)
    try:
        serializer = serializers.BackendVolumesSerializer(
            backend.get_volumes(), many=True
        )
    except (ConnectFailure, OpenStackBackendError) as e:
        raise exceptions.ValidationError(e)
    return response.Response(serializer.data, status=status.HTTP_200_OK)


openstack_views.TenantViewSet.backend_volumes = decorators.action(detail=True)(
    backend_volumes
)
