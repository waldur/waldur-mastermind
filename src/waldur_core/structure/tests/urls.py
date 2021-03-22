from django.conf.urls import include, url

from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.server.urls import urlpatterns

from . import views


def register_in(router):
    router.register(
        r'test-new-instances',
        views.TestNewInstanceViewSet,
        basename='test-new-instances',
    )


router = DefaultRouter()
register_in(router)

urlpatterns += [
    url(r'^api/', include(router.urls)),
]
