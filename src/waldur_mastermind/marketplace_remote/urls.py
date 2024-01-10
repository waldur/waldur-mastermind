from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r"^api/remote-waldur-api/remote_customers/$",
        views.CustomersView.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/shared_offerings/$",
        views.OfferingsListView.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/import_offering/$",
        views.OfferingCreateView.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_order/(?P<uuid>[a-f0-9]+)$",
        views.PullOrderView.as_view(),
        name="pull_remote_order",
    ),
    re_path(
        r"^api/remote-waldur-api/cancel_termination/(?P<uuid>[a-f0-9]+)$",
        views.CancelTerminationOrderView.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_details/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingDetails.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_users/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingUsers.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_resources/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingResources.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_orders/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingOrders.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_usage/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingUsage.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_invoices/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingInvoices.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/pull_offering_robot_accounts/(?P<uuid>[a-f0-9]+)/$",
        views.PullOfferingRobotAccounts.as_view(),
    ),
    re_path(
        r"^api/remote-waldur-api/push_project_data/(?P<uuid>[a-f0-9]+)/$",
        views.PushProjectData.as_view(),
    ),
]


def register_in(router):
    router.register(
        r"marketplace-project-update-requests",
        views.ProjectUpdateRequestViewSet,
        basename="marketplace-project-update-request",
    )
