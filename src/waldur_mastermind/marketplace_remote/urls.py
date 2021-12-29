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
]


def register_in(router):
    router.register(
        r'marketplace-project-update-requests',
        views.ProjectUpdateRequestViewSet,
        basename='marketplace-project-update-request',
    )
