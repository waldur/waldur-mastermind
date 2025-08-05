from django.conf.urls import include
from django.urls import re_path

from waldur_core.checklist import urls as checklist_urls
from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.server.urls import urlpatterns

from . import views


def register_in(router):
    router.register(
        r"test-new-instances",
        views.TestNewInstanceViewSet,
        basename="test-new-instances",
    )


router = DefaultRouter()
register_in(router)
checklist_urls.register_in(router)

urlpatterns += [
    re_path(r"^api/", include(router.urls)),
    re_path(r"^api/", include("waldur_core.checklist.urls")),
]
