from django.conf.urls import url

from waldur_mastermind.billing import views

urlpatterns = [
    url(r'^api/billing-total-cost/$', views.TotalCustomerCostView.as_view()),
]
