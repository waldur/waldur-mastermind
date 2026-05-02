from django.urls import include, re_path
from rest_framework.routers import SimpleRouter

from . import discovery_views, views

# Dedicated router for OpenStack settings discovery
settings_discovery_router = SimpleRouter()
settings_discovery_router.register(
    r"discovery",
    discovery_views.OpenStackDiscoveryViewSet,
    basename="openstack-discovery",
)


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
        r"openstack-external-networks",
        views.ExternalNetworkViewSet,
        basename="openstack-external-network",
    )
    router.register(
        r"openstack-hypervisors",
        views.HypervisorViewSet,
        basename="openstack-hypervisor",
    )
    router.register(
        r"openstack-hypervisor-inventories",
        views.HypervisorInventoryViewSet,
        basename="openstack-hypervisor-inventory",
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
        r"openstack-loadbalancers",
        views.LoadBalancerViewSet,
        basename="openstack-loadbalancer",
    )
    router.register(
        r"openstack-pools",
        views.PoolViewSet,
        basename="openstack-pool",
    )
    router.register(
        r"openstack-pool-members",
        views.PoolMemberViewSet,
        basename="openstack-poolmember",
    )
    router.register(
        r"openstack-health-monitors",
        views.HealthMonitorViewSet,
        basename="openstack-healthmonitor",
    )
    router.register(
        r"openstack-listeners",
        views.ListenerViewSet,
        basename="openstack-listener",
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
        r"openstack-volume-availability-zones",
        views.VolumeAvailabilityZoneViewSet,
        basename="openstack-volume-availability-zone",
    )

    router.register(
        r"openstack-network-rbac-policies",
        views.NetworkRBACPolicyViewSet,
        basename="openstack-network-rbac-policy",
    )


urlpatterns = [
    re_path(
        r"^api/openstack/",
        include(settings_discovery_router.urls),
    ),
]
