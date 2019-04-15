from django.conf.urls import url

from . import views


urlpatterns = [
    url(r'^api-auth/bcc/user-details/$', views.UserDetailsViewSet.as_view())
]
