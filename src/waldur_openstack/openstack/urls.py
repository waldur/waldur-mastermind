from . import views


def register_in(router):
    router.register(r'openstack', views.OpenStackServiceViewSet, base_name='openstack')
    router.register(r'openstack-images', views.ImageViewSet, base_name='openstack-image')
    router.register(r'openstack-flavors', views.FlavorViewSet, base_name='openstack-flavor')
    router.register(r'openstack-tenants', views.TenantViewSet, base_name='openstack-tenant')
    router.register(r'openstack-service-project-link', views.OpenStackServiceProjectLinkViewSet, base_name='openstack-spl')
    router.register(r'openstack-security-groups', views.SecurityGroupViewSet, base_name='openstack-sgp')
    router.register(r'openstack-floating-ips', views.FloatingIPViewSet, base_name='openstack-fip')
    router.register(r'openstack-networks', views.NetworkViewSet, base_name='openstack-network')
    router.register(r'openstack-subnets', views.SubNetViewSet, base_name='openstack-subnet')
