from constance import config
from django.conf import settings
from django.conf.urls import include
from django.contrib import admin
from django.urls import path, re_path

from waldur_core.core import WaldurExtension
from waldur_core.core import views as core_views
from waldur_core.core.logos import DEFAULT_LOGOS, LOGO_MAP
from waldur_core.core.nested_routers import NestedSimpleRouter
from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.logging import urls as logging_urls
from waldur_core.permissions import urls as permissions_urls
from waldur_core.structure import urls as structure_urls
from waldur_core.users import urls as users_urls
from waldur_mastermind.marketplace.views import (
    ServiceProviderCustomerProjectsViewSet,
    ServiceProviderCustomersViewSet,
    ServiceProviderKeysViewSet,
    ServiceProviderOfferingsViewSet,
    ServiceProviderProjectPermissionsViewSet,
    ServiceProviderProjectsViewSet,
    ServiceProviderUserCustomersViewSet,
    ServiceProviderUsersViewSet,
)

router = DefaultRouter()
logging_urls.register_in(router)
permissions_urls.register_in(router)
structure_urls.register_in(router)
users_urls.register_in(router)

urlpatterns = [
    re_path(r"^admin/", admin.site.urls),
    re_path(r"^admintools/", include("admin_tools.urls")),
    re_path(r"^health-check/", include("health_check.urls")),
    re_path(r"^api/celery-stats/", core_views.CeleryStatsViewSet.as_view()),
    re_path(r"^api/database-stats/", core_views.DatabaseStatsViewSet.as_view()),
    re_path(r"^api/query/", core_views.QueryViewSet.as_view()),
]

if settings.WALDUR_CORE.get("EXTENSIONS_AUTOREGISTER"):
    for ext in WaldurExtension.get_extensions():
        if ext.django_app() in settings.INSTALLED_APPS:
            urlpatterns += ext.django_urls()
            ext.rest_urls()(router)

service_provider_router = NestedSimpleRouter(
    router, r"marketplace-service-providers", lookup="service_provider"
)
service_provider_router.register(
    r"customers", ServiceProviderCustomersViewSet, basename="service-provider-customers"
)
service_provider_router.register(
    r"customer_projects",
    ServiceProviderCustomerProjectsViewSet,
    basename="service-provider-customer-projects",
)
service_provider_router.register(
    r"projects",
    ServiceProviderProjectsViewSet,
    basename="service-provider-projects",
)
service_provider_router.register(
    r"project_permissions",
    ServiceProviderProjectPermissionsViewSet,
    basename="service-provider-project-permissions",
)
service_provider_router.register(
    r"keys",
    ServiceProviderKeysViewSet,
    basename="service-provider-keys",
)
service_provider_router.register(
    r"users",
    ServiceProviderUsersViewSet,
    basename="service-provider-users",
)
service_provider_router.register(
    r"user_customers",
    ServiceProviderUserCustomersViewSet,
    basename="service-provider-user-customers",
)
service_provider_router.register(
    r"offerings",
    ServiceProviderOfferingsViewSet,
    basename="service-provider-offerings",
)

urlpatterns += [
    re_path(r"^api/", include(router.urls)),
    re_path(r"^api/", include(service_provider_router.urls)),
    re_path(r"^api/", include("waldur_core.logging.urls")),
    re_path(r"^api/", include("waldur_core.media.urls")),
    re_path(r"^api/", include("waldur_core.structure.urls")),
    re_path(r"^api/configuration/", core_views.configuration_detail),
    re_path(r"^api/override-settings/", core_views.override_db_settings),
    re_path(r"^api/version/", core_views.version_detail),
    re_path(r"^api/feature-values/", core_views.feature_values),
    re_path(
        r"^api-auth/password/",
        core_views.ObtainAuthToken.as_view(),
        name="auth-password",
    ),
    re_path(r"^api-auth/logout/", core_views.LogoutView.as_view(), name="auth-logout"),
    re_path(
        r"^$",
        core_views.ExtraContextTemplateView.as_view(
            template_name="landing/index.html",
            extra_context={"site_name": config.SITE_NAME},
        ),
    ),
]

for key, val in LOGO_MAP.items():
    urlpatterns.append(
        path(
            val,
            core_views.get_whitelabeling_logo,
            kwargs={
                "logo_type": key,
                "default_image": DEFAULT_LOGOS.get(key),
            },
        )
    )


if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # enable login/logout for web UI in debug mode
    urlpatterns += (
        re_path(
            r"^api-auth/", include("rest_framework.urls", namespace="rest_framework")
        ),
    )
