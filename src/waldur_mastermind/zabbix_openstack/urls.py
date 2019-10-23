from . import views


def register_in(router):
    router.register(r'zabbix-openstack-links', views.LinkViewSet, basename='zabbix-openstack-links')


urlpatterns = []
