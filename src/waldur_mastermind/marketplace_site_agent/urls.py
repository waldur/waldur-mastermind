from django.urls import re_path

from . import views


def register_in(router):
    router.register(
        r"marketplace-site-agent-identities",
        views.AgentIdentityViewSet,
        basename="marketplace-site-agent-identity",
    )
    router.register(
        r"marketplace-site-agent-services",
        views.AgentServiceViewSet,
        basename="marketplace-site-agent-service",
    )
    router.register(
        r"marketplace-site-agent-processors",
        views.AgentProcessorViewSet,
        basename="marketplace-site-agent-processor",
    )
    router.register(
        r"marketplace-site-agent-logs",
        views.SiteAgentLogViewSet,
        basename="marketplace-site-agent-log",
    )


urlpatterns = [
    re_path(
        r"^api/projects/(?P<uuid>[a-f0-9]+)/sync_user_roles/$",
        views.ProjectSyncUserRolesView.as_view(),
        name="project-sync-user-roles",
    ),
    re_path(
        r"^api/marketplace-site-agent-stats/$",
        views.AgentStatsViewSet.as_view(),
        name="marketplace-site-agent-stats",
    ),
    re_path(
        r"^api/marketplace-site-agent-task-stats/$",
        views.AgentTaskStatsViewSet.as_view(),
        name="marketplace-site-agent-task-stats",
    ),
    re_path(
        r"^api/marketplace-site-agent-connection-stats/$",
        views.AgentConnectionStatsViewSet.as_view(),
        name="marketplace-site-agent-connection-stats",
    ),
]
