from django.urls import re_path

from waldur_core.media import views

urlpatterns = [
    re_path(
        r"^media/(?P<uuid>.+)/$",
        views.MediaView.as_view(),
        name="media",
    ),
]
