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


urlpatterns = [
    re_path(
        r"^projects/(?P<uuid>[a-f0-9]+)/sync_user_roles/$",
        views.ProjectSyncUserRolesView.as_view(),
        name="project-sync-user-roles",
    ),
]
