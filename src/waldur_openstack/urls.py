from django.urls import re_path

from . import views


def register_in(router):
    router.register(r"openstack-images", views.ImageViewSet, basename="openstack-image")
    router.register(
        r"openstack-flavors", views.FlavorViewSet, basename="openstack-flavor"
    )
    router.register(
        r"openstack-volume-types",
        views.VolumeTypeViewSet,
        basename="openstack-volume-type",
    )
    router.register(
        r"openstack-tenants", views.TenantViewSet, basename="openstack-tenant"
    )
    router.register(
        r"openstack-security-groups",
        views.SecurityGroupViewSet,
        basename="openstack-sgp",
    )
    router.register(
        r"openstack-server-groups",
        views.ServerGroupViewSet,
        basename="openstack-server-group",
    )
    router.register(r"openstack-ports", views.PortViewSet, basename="openstack-port")
    router.register(
        r"openstack-floating-ips", views.FloatingIPViewSet, basename="openstack-fip"
    )
    router.register(
        r"openstack-routers", views.RouterViewSet, basename="openstack-router"
    )
    router.register(
        r"openstack-networks", views.NetworkViewSet, basename="openstack-network"
    )
    router.register(
        r"openstack-subnets", views.SubNetViewSet, basename="openstack-subnet"
    )
    router.register(
        r"openstack-volumes",
        views.VolumeViewSet,
        basename="openstack-volume",
    )
    router.register(
        r"openstack-snapshots",
        views.SnapshotViewSet,
        basename="openstack-snapshot",
    )
    router.register(
        r"openstack-instance-availability-zones",
        views.InstanceAvailabilityZoneViewSet,
        basename="openstack-instance-availability-zone",
    )
    router.register(
        r"openstack-instances",
        views.InstanceViewSet,
        basename="openstack-instance",
    )
    router.register(
        r"openstack-backups",
        views.BackupViewSet,
        basename="openstack-backup",
    )
    router.register(
        r"openstack-backup-schedules",
        views.BackupScheduleViewSet,
        basename="openstack-backup-schedule",
    )
    router.register(
        r"openstack-snapshot-schedules",
        views.SnapshotScheduleViewSet,
        basename="openstack-snapshot-schedule",
    )
    router.register(
        r"openstack-volume-availability-zones",
        views.VolumeAvailabilityZoneViewSet,
        basename="openstack-volume-availability-zone",
    )


urlpatterns = [
    re_path(
        r"^api/openstack-shared-settings-instances/$",
        views.SharedSettingsInstances.as_view(),
    ),
    re_path(
        r"^api/openstack-shared-settings-customers/$",
        views.SharedSettingsCustomers.as_view(),
    ),
]
