from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'^api/remote-waldur-api/remote_customers/$', views.CustomersView.as_view(),),
    url(
        r'^api/remote-waldur-api/shared_offerings/$', views.OfferingsListView.as_view(),
    ),
    url(
        r'^api/remote-waldur-api/import_offering/$', views.OfferingCreateView.as_view(),
    ),
    url(
        r'^api/remote-waldur-api/pull_order_item/(?P<uuid>[a-f0-9]+)$',
        views.PullOrderItemView.as_view(),
        name='pull_remote_order_item',
    ),
    url(
        r'^api/remote-waldur-api/pull_offering_details/(?P<uuid>[a-f0-9]+)/$',
        views.pull_offering_details,
    ),
    url(
        r'^api/remote-waldur-api/pull_offering_users/(?P<uuid>[a-f0-9]+)/$',
        views.pull_offering_users,
    ),
    url(
        r'^api/remote-waldur-api/pull_offering_resources/(?P<uuid>[a-f0-9]+)/$',
        views.pull_offering_resources,
    ),
    url(
        r'^api/remote-waldur-api/pull_offering_order_items/(?P<uuid>[a-f0-9]+)/$',
        views.pull_offering_order_items,
    ),
    url(
        r'^api/remote-waldur-api/pull_offering_usage/(?P<uuid>[a-f0-9]+)/$',
        views.pull_offering_usage,
    ),
    url(
        r'^api/remote-waldur-api/pull_offering_invoices/(?P<uuid>[a-f0-9]+)/$',
        views.pull_offering_invoices,
    ),
]


def register_in(router):
    router.register(
        r'marketplace-project-update-requests',
        views.ProjectUpdateRequestViewSet,
        basename='marketplace-project-update-request',
    )
