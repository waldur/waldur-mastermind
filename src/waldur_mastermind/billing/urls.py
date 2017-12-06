from __future__ import unicode_literals

from django.conf.urls import url

from waldur_mastermind.billing import views


def register_in(router):
    router.register(r'billing-price-estimates', views.PriceEstimateViewSet, base_name='billing-price-estimate')


urlpatterns = [
    url(r'^api/billing-total-cost/$', views.TotalCustomerCostView.as_view()),
]
