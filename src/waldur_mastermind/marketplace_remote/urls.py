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
]
