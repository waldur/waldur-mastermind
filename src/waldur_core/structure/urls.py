from waldur_core.core import views as core_views
from waldur_core.structure import views


def register_in(router):
    router.register(
        r"personal-access-tokens",
        core_views.PersonalAccessTokenViewSet,
        basename="personal-access-token",
    )
    router.register(r"customers", views.CustomerViewSet)
    router.register(r"project-types", views.ProjectTypeViewSet, basename="project_type")
    router.register(r"projects", views.ProjectViewSet)
    router.register(
        r"customer-permissions-reviews",
        views.CustomerPermissionReviewViewSet,
        basename="customer_permission_review",
    )
    router.register(
        r"project-permissions-reviews",
        views.ProjectPermissionReviewViewSet,
        basename="project-permissions-review",
    )
    router.register(
        r"project-end-date-change-requests",
        views.ProjectEndDateChangeRequestViewSet,
        basename="project-end-date-change-request",
    )
    router.register(r"service-settings", views.ServiceSettingsViewSet)
    router.register(r"users", views.UserViewSet)
    router.register(r"keys", views.SshKeyViewSet)
    router.register(
        r"organization-groups",
        views.OrganizationGroupViewSet,
        basename="organization-group",
    )
    router.register(
        r"affiliated-organizations",
        views.AffiliatedOrganizationViewSet,
        basename="affiliated-organization",
    )
    router.register(
        r"user-agreements",
        views.UserAgreementsViewSet,
        basename="user-agreements",
    )
    router.register(
        r"notification-messages",
        views.NotificationViewSet,
        basename="notification-messages",
    )
    router.register(
        r"notification-messages-templates",
        views.NotificationTemplateViewSet,
        basename="notification-messages-templates",
    )
    router.register(
        r"auth-tokens",
        views.AuthTokenViewSet,
        basename="auth-tokens",
    )
    router.register(
        r"access-subnets",
        views.AccessSubnetViewSet,
        basename="access-subnets",
    )
    router.register(
        r"external-links",
        views.ExternalLinkViewSet,
        basename="external-links",
    )


urlpatterns = []
