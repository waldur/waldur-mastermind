from . import views


def register_in(router):
    router.register(r'openstacktenant', views.OpenStackServiceViewSet, base_name='openstacktenant')
    router.register(r'openstacktenant-service-project-link', views.OpenStackServiceProjectLinkViewSet,
                    base_name='openstacktenant-spl')
    router.register(r'openstacktenant-images', views.ImageViewSet, base_name='openstacktenant-image')
    router.register(r'openstacktenant-flavors', views.FlavorViewSet, base_name='openstacktenant-flavor')
    router.register(r'openstacktenant-floating-ips', views.FloatingIPViewSet, base_name='openstacktenant-fip')
    router.register(r'openstacktenant-security-groups', views.SecurityGroupViewSet, base_name='openstacktenant-sgp')
    router.register(r'openstacktenant-volumes', views.VolumeViewSet, base_name='openstacktenant-volume')
    router.register(r'openstacktenant-snapshots', views.SnapshotViewSet, base_name='openstacktenant-snapshot')
    router.register(r'openstacktenant-instances', views.InstanceViewSet, base_name='openstacktenant-instance')
    router.register(r'openstacktenant-backups', views.BackupViewSet, base_name='openstacktenant-backup')
    router.register(r'openstacktenant-backup-schedules', views.BackupScheduleViewSet,
                    base_name='openstacktenant-backup-schedule')
    router.register(r'openstacktenant-snapshot-schedules', views.SnapshotScheduleViewSet,
                    base_name='openstacktenant-snapshot-schedule')
    router.register(r'openstacktenant-subnets', views.SubNetViewSet, base_name='openstacktenant-subnet')
    router.register(r'openstacktenant-networks', views.NetworkViewSet, base_name='openstacktenant-network')
