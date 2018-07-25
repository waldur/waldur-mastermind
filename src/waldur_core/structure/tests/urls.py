from django.conf.urls import include, url

from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.server.urls import urlpatterns

from . import views


def register_in(router):
    router.register(r'test', views.TestServiceViewSet, base_name='test')
    router.register(r'test-service-project-link', views.TestServiceProjectLinkViewSet, base_name='test-spl')
    router.register(r'test-new-instances', views.TestNewInstanceViewSet, base_name='test-new-instances')


router = DefaultRouter()
register_in(router)

urlpatterns += [
    url(r'^api/', include(router.urls)),
]
