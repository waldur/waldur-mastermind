from django.urls import re_path

from waldur_core.media import views

urlpatterns = [
    re_path(
        r"^media-download/(?P<token>.+)/$",
        views.ProtectedFileView.as_view(),
        name="media-download",
    ),
]
