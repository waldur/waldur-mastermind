from constance import config
from django.conf import settings
from django.conf.urls import include
from django.contrib import admin
from django.urls import path, re_path

from waldur_core.checklist import urls as checklist_urls
from waldur_core.core import WaldurExtension
from waldur_core.core import views as core_views
from waldur_core.core.logos import DEFAULT_LOGOS, LOGO_MAP
from waldur_core.core.nested_routers import NestedSimpleRouter
from waldur_core.core.routers import SortedDefaultRouter as DefaultRouter
from waldur_core.logging import urls as logging_urls
from waldur_core.onboarding import urls as onboarding_urls
from waldur_core.permissions import urls as permissions_urls
from waldur_core.structure import urls as structure_urls
from waldur_core.structure.views import (
    CustomerProjectMetadataComplianceDetailsViewSet,
    CustomerProjectMetadataComplianceOverviewViewSet,
    CustomerProjectMetadataComplianceProjectsViewSet,
    CustomerProjectMetadataQuestionAnswersViewSet,
    CustomerUsersViewSet,
    ProjectOtherUsersViewSet,
)
from waldur_core.user_actions import urls as user_actions_urls
from waldur_core.users import urls as users_urls
from waldur_mastermind.marketplace.views import (
    ServiceProviderComplianceViewSet,
    ServiceProviderCourseAccountsViewSet,
    ServiceProviderCustomerProjectsViewSet,
    ServiceProviderCustomersViewSet,
    ServiceProviderKeysViewSet,
    ServiceProviderOfferingsViewSet,
    ServiceProviderProjectPermissionsViewSet,
    ServiceProviderProjectServiceAccountsViewSet,
    ServiceProviderProjectsViewSet,
    ServiceProviderUserCustomersViewSet,
    ServiceProviderUsersViewSet,
)
from waldur_mastermind.proposal.views import (
    ReviewerProfileAffiliationViewSet,
    ReviewerProfileExpertiseViewSet,
    ReviewerProfilePublicationViewSet,
)

router = DefaultRouter()
logging_urls.register_in(router)
onboarding_urls.register_in(router)
permissions_urls.register_in(router)
structure_urls.register_in(router)
users_urls.register_in(router)
user_actions_urls.register_in(router)
checklist_urls.register_in(router)

urlpatterns = [
    re_path(r"^admin/", admin.site.urls),
    re_path(r"^health-check/", include("health_check.urls")),
    re_path(r"^scim/v2/", include("waldur_core.users.scim.server.urls")),
    # Stats endpoints (consolidated under /api/stats/)
    re_path(r"^api/stats/celery/", core_views.CeleryStatsViewSet.as_view()),
    re_path(r"^api/stats/database/", core_views.DatabaseStatsViewSet.as_view()),
    re_path(r"^api/stats/query/", core_views.QueryViewSet.as_view()),
    re_path(r"^api/stats/table-growth/", core_views.TableGrowthStatsViewSet.as_view()),
    # Legacy endpoints (deprecated - use /api/stats/* instead)
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
    r"project_service_accounts",
    ServiceProviderProjectServiceAccountsViewSet,
    basename="service-provider-project-service-accounts",
)
service_provider_router.register(
    r"course_accounts",
    ServiceProviderCourseAccountsViewSet,
    basename="service-provider-course-accounts",
)
service_provider_router.register(
    r"offerings",
    ServiceProviderOfferingsViewSet,
    basename="service-provider-offerings",
)
service_provider_router.register(
    r"compliance",
    ServiceProviderComplianceViewSet,
    basename="service-provider-compliance",
)

customer_router = NestedSimpleRouter(router, r"customers", lookup="customer")
customer_router.register(
    r"users",
    CustomerUsersViewSet,
    basename="customer-users",
)
customer_router.register(
    r"project-metadata-compliance-overview",
    CustomerProjectMetadataComplianceOverviewViewSet,
    basename="customer-project-metadata-compliance-overview",
)
customer_router.register(
    r"project-metadata-compliance-details",
    CustomerProjectMetadataComplianceDetailsViewSet,
    basename="customer-project-metadata-compliance-details",
)
customer_router.register(
    r"project-metadata-compliance-projects",
    CustomerProjectMetadataComplianceProjectsViewSet,
    basename="customer-project-metadata-compliance-projects",
)
customer_router.register(
    r"project-metadata-question-answers",
    CustomerProjectMetadataQuestionAnswersViewSet,
    basename="customer-project-metadata-question-answers",
)

project_router = NestedSimpleRouter(router, r"projects", lookup="project")
project_router.register(
    r"other_users",
    ProjectOtherUsersViewSet,
    basename="project-other-users",
)

reviewer_profile_router = NestedSimpleRouter(
    router, r"reviewer-profiles", lookup="reviewer_profile"
)
reviewer_profile_router.register(
    r"affiliations",
    ReviewerProfileAffiliationViewSet,
    basename="reviewer-profile-affiliations",
)
reviewer_profile_router.register(
    r"expertise",
    ReviewerProfileExpertiseViewSet,
    basename="reviewer-profile-expertise",
)
reviewer_profile_router.register(
    r"publications",
    ReviewerProfilePublicationViewSet,
    basename="reviewer-profile-publications",
)


urlpatterns += [
    re_path(r"^api/", include(router.urls)),
    re_path(r"^api/", include(service_provider_router.urls)),
    re_path(r"^api/", include(customer_router.urls)),
    re_path(r"^api/", include(project_router.urls)),
    re_path(r"^api/", include(reviewer_profile_router.urls)),
    re_path(r"^api/", include("waldur_core.logging.urls")),
    re_path(r"^api/", include("waldur_core.media.urls")),
    re_path(r"^api/", include("waldur_core.structure.urls")),
    re_path(r"^api/", include("waldur_core.checklist.urls")),
    re_path(r"^api/", include("waldur_core.user_actions.urls")),
    re_path(r"^api/", include(onboarding_urls)),
]


urlpatterns += [
    re_path(r"^api/configuration/", core_views.configuration_detail),
    re_path(r"^api/override-settings/", core_views.override_db_settings),
    re_path(r"^api/version/", core_views.version_detail),
    re_path(r"^api/feature-values/", core_views.feature_values),
    re_path(r"^api/metadata/permissions/", core_views.PermissionMetadataView.as_view()),
    re_path(r"^api/metadata/events/", core_views.EventMetadataView.as_view()),
    re_path(r"^api/metadata/features/", core_views.FeatureMetadataView.as_view()),
    re_path(r"^api/metadata/settings/", core_views.SettingsMetadataView.as_view()),
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
            extra_context={"site_name": lambda: config.SITE_NAME},
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
    # Using different path to avoid conflict with custom logout at /api-auth/logout/
    urlpatterns += (
        re_path(
            r"^api-auth-browsable/",
            include("rest_framework.urls", namespace="rest_framework"),
        ),
    )
