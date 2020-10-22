from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'^api/geocode/$', views.GeocodeViewSet.as_view(), name='geocode'),
]
