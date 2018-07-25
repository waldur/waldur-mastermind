from __future__ import unicode_literals

from django.conf import settings
from django.conf.urls import include, url
from django.contrib import admin
from django.views.generic import TemplateView

from waldur_core.core import WaldurExtension
from waldur_core.core import views as core_views
from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.core.schemas import WaldurSchemaView
from waldur_core.cost_tracking import urls as cost_tracking_urls, CostTrackingRegister
from waldur_core.logging import urls as logging_urls
from waldur_core.monitoring import urls as monitoring_urls
from waldur_core.quotas import urls as quotas_urls
from waldur_core.structure import urls as structure_urls
from waldur_core.users import urls as users_urls

CostTrackingRegister.autodiscover()

router = DefaultRouter()
cost_tracking_urls.register_in(router)
logging_urls.register_in(router)
monitoring_urls.register_in(router)
quotas_urls.register_in(router)
structure_urls.register_in(router)
users_urls.register_in(router)


urlpatterns = [
    url(r'^admin/', admin.site.urls),
    url(r'^admintools/', include('admin_tools.urls')),
    url(r'^admin/defender/', include('defender.urls')),
]

if settings.WALDUR_CORE.get('EXTENSIONS_AUTOREGISTER'):
    for ext in WaldurExtension.get_extensions():
        if ext.django_app() in settings.INSTALLED_APPS:
            urlpatterns += ext.django_urls()
            ext.rest_urls()(router)

urlpatterns += [
    url(r'^docs/', WaldurSchemaView.as_view()),
    url(r'^api/', include(router.urls)),
    url(r'^api/', include('waldur_core.logging.urls')),
    url(r'^api/', include('waldur_core.structure.urls')),
    url(r'^api/version/', core_views.version_detail),
    url(r'^api/configuration/', core_views.configuration_detail),
    url(r'^api-auth/password/', core_views.obtain_auth_token, name='auth-password'),
    url(r'^$', TemplateView.as_view(template_name='landing/index.html')),
]

if settings.DEBUG:
    from django.conf.urls.static import static
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # enable login/logout for web UI in debug mode
    urlpatterns += url(r'^api-auth/', include('rest_framework.urls', namespace='rest_framework')),
