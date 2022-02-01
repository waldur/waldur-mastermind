from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r'^api-auth/smartidee/$', views.SmartIDeeView.as_view(), name='auth_smartidee'
    ),
    re_path(r'^api-auth/tara/$', views.TARAView.as_view(), name='auth_tara'),
    re_path(
        r'^api-auth/keycloak/$', views.KeycloakView.as_view(), name='auth_keycloak'
    ),
    re_path(
        r'^api-auth/eduteams/$', views.EduteamsView.as_view(), name='auth_eduteams'
    ),
    re_path(
        r'^api/remote-eduteams/$',
        views.RemoteEduteamsView.as_view(),
        name='auth_remote_eduteams',
    ),
]
