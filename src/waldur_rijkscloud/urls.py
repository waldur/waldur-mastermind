from . import views


def register_in(router):
    router.register(r'rijkscloud', views.ServiceViewSet, base_name='rijkscloud')
    router.register(r'rijkscloud-service-project-link', views.ServiceProjectLinkViewSet,
                    base_name='rijkscloud-spl')
    router.register(r'rijkscloud-flavors', views.FlavorViewSet, base_name='rijkscloud-flavor')
    router.register(r'rijkscloud-volumes', views.VolumeViewSet, base_name='rijkscloud-volume')
    router.register(r'rijkscloud-instances', views.InstanceViewSet, base_name='rijkscloud-instance')
    router.register(r'rijkscloud-networks', views.NetworkViewSet, base_name='rijkscloud-network')
    router.register(r'rijkscloud-subnets', views.SubNetViewSet, base_name='rijkscloud-subnet')
    router.register(r'rijkscloud-internal-ips', views.InternalIPViewSet, base_name='rijkscloud-internal-ip')
    router.register(r'rijkscloud-floating-ips', views.FloatingIPViewSet, base_name='rijkscloud-fip')
