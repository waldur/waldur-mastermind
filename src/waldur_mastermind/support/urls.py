from django.urls import include, re_path
from rest_framework.routers import SimpleRouter

from waldur_mastermind.support import views

# Create a dedicated router for settings discovery
# This creates the hierarchical URL structure: /api/support/settings/atlassian/
settings_router = SimpleRouter()
settings_router.register(
    r"atlassian",
    views.AtlassianSettingsDiscoveryViewSet,
    basename="support-settings-atlassian",
)


def register_in(router):
    router.register(r"support-issues", views.IssueViewSet, basename="support-issue")
    router.register(
        r"support-priorities", views.PriorityViewSet, basename="support-priority"
    )
    router.register(
        r"support-request-types",
        views.RequestTypeViewSet,
        basename="support-request-type",
    )
    router.register(
        r"support-request-types-admin",
        views.RequestTypeAdminViewSet,
        basename="support-request-type-admin",
    )
    router.register(
        r"support-comments", views.CommentViewSet, basename="support-comment"
    )
    router.register(r"support-users", views.SupportUserViewSet, basename="support-user")
    router.register(
        r"support-attachments", views.AttachmentViewSet, basename="support-attachment"
    )
    router.register(
        r"support-templates", views.TemplateViewSet, basename="support-template"
    )
    router.register(
        r"support-feedbacks",
        views.FeedbackViewSet,
        basename="support-feedback",
    )
    router.register(
        r"support-issue-statuses",
        views.IssueStatusViewSet,
        basename="support-issue-status",
    )


urlpatterns = [
    # Settings discovery endpoints under /api/support/settings/
    re_path(r"^api/support/settings/", include(settings_router.urls)),
    re_path(
        r"^api/support-jira-webhook/$",
        views.WebHookReceiverView.as_view(),
        name="web-hook-receiver",
    ),
    re_path(
        r"^api/support-feedback-report/$",
        views.FeedbackReportViewSet.as_view(),
        name="support-feedback-report",
    ),
    re_path(
        r"^api/support-feedback-average-report/$",
        views.FeedbackAverageReportViewSet.as_view(),
        name="support-feedback-average-report",
    ),
    re_path(
        r"^api/support-zammad-webhook/$",
        views.ZammadWebHookReceiverView.as_view(),
        name="zammad-web-hook-receiver",
    ),
    re_path(
        r"^api/support-smax-webhook/$",
        views.SmaxWebHookReceiverView.as_view(),
        name="smax-web-hook-receiver",
    ),
    re_path(
        r"^api/sync-issues/$",
        views.sync_issues,
        name="sync-issues",
    ),
    re_path(
        r"^api/support-statistics/$",
        views.SupportStatsViewSet.as_view(),
        name="support-statistics",
    ),
]
