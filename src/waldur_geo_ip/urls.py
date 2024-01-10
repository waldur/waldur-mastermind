from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^api/geocode/$", views.GeocodeViewSet.as_view(), name="geocode"),
]
