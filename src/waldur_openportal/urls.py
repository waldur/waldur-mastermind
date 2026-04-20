from django.urls import re_path

from . import views
from .api import (
    access_for_email,
    customer_spend_info,
    fetch_job,
    get_api_token,
    offering_mapping,
    project_mapping,
    project_spend_info,
    user_mapping,
    whoami,
)


def register_in(router):
    router.register(
        r"openportal-allocations",
        views.AllocationViewSet,
        basename="openportal-allocation",
    )
    router.register(
        r"openportal-remote-allocations",
        views.RemoteAllocationViewSet,
        basename="openportal-remote-allocation",
    )
    router.register(
        r"openportal-allocation-user-usage",
        views.AllocationUserUsageViewSet,
        basename="openportal-allocation-user-usage",
    )
    router.register(
        r"openportal-project-usage-reports",
        views.CachedProjectUsageReportViewSet,
        basename="openportal-project-usage-report",
    )
    router.register(
        r"openportal-project-storage-reports",
        views.CachedProjectStorageReportViewSet,
        basename="openportal-project-storage-report",
    )
    router.register(
        r"openportal-associations",
        views.AssociationViewSet,
        basename="openportal-association",
    )
    router.register(
        r"openportal-remote-associations",
        views.RemoteAssociationViewSet,
        basename="openportal-remote-association",
    )
    router.register(
        r"openportal-userinfo",
        views.UserInfoViewSet,
        basename="openportal-userinfo",
    )
    router.register(
        r"openportal-projectinfo",
        views.ProjectInfoViewSet,
        basename="openportal-projectinfo",
    )
    router.register(
        r"openportal-project-template",
        views.ProjectTemplateViewSet,
        basename="openportal-project-template",
    )
    router.register(
        r"openportal-managed-projects",
        views.ManagedProjectViewSet,
        basename="openportal-managed-project",
    )
    router.register(
        r"openportal-unmanaged-projects",
        views.UnmanagedProjectViewSet,
        basename="openportal-unmanaged-project",
    )
    router.register(
        r"openportal-accounting-summary",
        views.ProjectAccountingSummaryViewSet,
        basename="openportal-accounting-summary",
    )


urlpatterns = [
    re_path(
        r"^api/openportal/access_for_email/",
        access_for_email,
        name="access-for-email",
    ),
    re_path(
        r"^api/openportal/project_spend_info/",
        project_spend_info,
        name="project-spend-info",
    ),
    re_path(
        r"^api/openportal/customer_spend_info/",
        customer_spend_info,
        name="customer-spend-info",
    ),
    re_path(
        r"^api/openportal/fetch_job/",
        fetch_job,
        name="fetch-job",
    ),
    re_path(
        r"^api/openportal/whoami/",
        whoami,
        name="whoami",
    ),
    re_path(
        r"^api/openportal/get_api_token/",
        get_api_token,
        name="get_api_token",
    ),
    re_path(
        r"^api/openportal/offering_mapping/",
        offering_mapping,
        name="offering-mapping",
    ),
    re_path(
        r"^api/openportal/project_mapping/",
        project_mapping,
        name="project-mapping",
    ),
    re_path(
        r"^api/openportal/user_mapping/",
        user_mapping,
        name="user-mapping",
    ),
    # Custom routes for ManagedProject with composite lookup
    re_path(
        r"^api/openportal-managed-projects/(?P<identifier>[\w.-]+)/(?P<destination>[\w.-]+)/$",
        views.ManagedProjectViewSet.as_view({"get": "retrieve"}),
        name="openportal-managed-project-detail",
    ),
    re_path(
        r"^api/openportal-managed-projects/(?P<identifier>[\w.-]+)/(?P<destination>[\w.-]+)/approve/$",
        views.ManagedProjectViewSet.as_view({"post": "approve"}),
        name="openportal-managed-project-approve",
    ),
    re_path(
        r"^api/openportal-managed-projects/(?P<identifier>[\w.-]+)/(?P<destination>[\w.-]+)/reject/$",
        views.ManagedProjectViewSet.as_view({"post": "reject"}),
        name="openportal-managed-project-reject",
    ),
    re_path(
        r"^api/openportal-managed-projects/(?P<identifier>[\w.-]+)/(?P<destination>[\w.-]+)/delete/$",
        views.ManagedProjectViewSet.as_view({"delete": "delete"}),
        name="openportal-managed-project-delete",
    ),
    re_path(
        r"^api/openportal-managed-projects/(?P<identifier>[\w.-]+)/(?P<destination>[\w.-]+)/attach/$",
        views.ManagedProjectViewSet.as_view({"post": "attach"}),
        name="openportal-managed-project-attach",
    ),
    re_path(
        r"^api/openportal-managed-projects/(?P<identifier>[\w.-]+)/(?P<destination>[\w.-]+)/detach/$",
        views.ManagedProjectViewSet.as_view({"post": "detach"}),
        name="openportal-managed-project-detach",
    ),
]
