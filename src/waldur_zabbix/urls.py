from django.conf.urls import url

from . import views


def register_in(router):
    router.register(r'zabbix-hosts', views.HostViewSet, basename='zabbix-host')
    router.register(
        r'zabbix-itservices', views.ITServiceViewSet, basename='zabbix-itservice'
    )
    router.register(
        r'zabbix-templates', views.TemplateViewSet, basename='zabbix-template'
    )
    router.register(r'zabbix-triggers', views.TriggerViewSet, basename='zabbix-trigger')
    router.register(
        r'zabbix-user-groups', views.UserGroupViewSet, basename='zabbix-user-group'
    )
    router.register(r'zabbix-users', views.UserViewSet, basename='zabbix-user')


urlpatterns = [
    url(
        r'^api/zabbix-service-credentials/(?P<uuid>[a-f0-9]+)/$',
        views.ServiceCredentials.as_view(),
    ),
    url(
        r'^api/zabbix-service-trigger-status/(?P<uuid>[a-f0-9]+)/$',
        views.ServiceTriggerStatus.as_view(),
    ),
]
