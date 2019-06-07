from django.conf.urls import url

from waldur_core.media import views


urlpatterns = [
    url(r'^media-download/(?P<token>.+)/$',
        views.ProtectedFileView.as_view(),
        name='media-download'),
]
