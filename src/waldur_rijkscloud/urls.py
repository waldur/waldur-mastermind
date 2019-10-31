from . import views


def register_in(router):
    router.register(r'rijkscloud', views.ServiceViewSet, basename='rijkscloud')
    router.register(r'rijkscloud-service-project-link', views.ServiceProjectLinkViewSet,
                    basename='rijkscloud-spl')
    router.register(r'rijkscloud-flavors', views.FlavorViewSet, basename='rijkscloud-flavor')
    router.register(r'rijkscloud-volumes', views.VolumeViewSet, basename='rijkscloud-volume')
    router.register(r'rijkscloud-instances', views.InstanceViewSet, basename='rijkscloud-instance')
    router.register(r'rijkscloud-networks', views.NetworkViewSet, basename='rijkscloud-network')
    router.register(r'rijkscloud-subnets', views.SubNetViewSet, basename='rijkscloud-subnet')
    router.register(r'rijkscloud-internal-ips', views.InternalIPViewSet, basename='rijkscloud-internal-ip')
    router.register(r'rijkscloud-floating-ips', views.FloatingIPViewSet, basename='rijkscloud-fip')
