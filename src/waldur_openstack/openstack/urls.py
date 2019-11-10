from . import views


def register_in(router):
    router.register(r'openstack', views.OpenStackServiceViewSet, basename='openstack')
    router.register(r'openstack-images', views.ImageViewSet, basename='openstack-image')
    router.register(r'openstack-flavors', views.FlavorViewSet, basename='openstack-flavor')
    router.register(r'openstack-volume-types', views.VolumeTypeViewSet, basename='openstack-volume-type')
    router.register(r'openstack-tenants', views.TenantViewSet, basename='openstack-tenant')
    router.register(r'openstack-service-project-link', views.OpenStackServiceProjectLinkViewSet, basename='openstack-spl')
    router.register(r'openstack-security-groups', views.SecurityGroupViewSet, basename='openstack-sgp')
    router.register(r'openstack-floating-ips', views.FloatingIPViewSet, basename='openstack-fip')
    router.register(r'openstack-networks', views.NetworkViewSet, basename='openstack-network')
    router.register(r'openstack-subnets', views.SubNetViewSet, basename='openstack-subnet')
