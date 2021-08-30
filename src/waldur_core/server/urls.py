from django.conf import settings
from django.conf.urls import include, url
from django.contrib import admin

from waldur_core.core import WaldurExtension
from waldur_core.core import views as core_views
from waldur_core.core.api_groups_mapping import API_GROUPS
from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.core.schemas import WaldurSchemaView
from waldur_core.logging import urls as logging_urls
from waldur_core.quotas import urls as quotas_urls
from waldur_core.structure import urls as structure_urls
from waldur_core.users import urls as users_urls

router = DefaultRouter()
logging_urls.register_in(router)
quotas_urls.register_in(router)
structure_urls.register_in(router)
users_urls.register_in(router)


urlpatterns = [
    url(r'^admin/', admin.site.urls),
    url(r'^admintools/', include('admin_tools.urls')),
    url(r'^health-check/', include('health_check.urls')),
    url(r'^media/', include('binary_database_files.urls')),
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
    url(r'^api/', include('waldur_core.media.urls')),
    url(r'^api/', include('waldur_core.structure.urls')),
    url(r'^api/version/', core_views.version_detail),
    url(r'^api/configuration/', core_views.configuration_detail),
    url(r'^api/features-description/', core_views.features_description),
    url(r'^api/feature-values/', core_views.feature_values),
    url(r'^api-auth/password/', core_views.obtain_auth_token, name='auth-password'),
    url(
        r'^$',
        core_views.ExtraContextTemplateView.as_view(
            template_name='landing/index.html',
            extra_context={'site_name': settings.WALDUR_CORE['SITE_NAME']},
        ),
    ),
    url(
        r'^apidocs$',
        core_views.ExtraContextTemplateView.as_view(
            template_name='landing/apidocs.html',
            extra_context={
                'api_groups': sorted(API_GROUPS.keys()),
                'site_name': settings.WALDUR_CORE['SITE_NAME'],
            },
        ),
    ),
]

if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # enable login/logout for web UI in debug mode
    urlpatterns += (
        url(r'^api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    )
