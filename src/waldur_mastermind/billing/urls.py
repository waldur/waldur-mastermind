from django.urls import re_path

from waldur_mastermind.billing import views

urlpatterns = [
    re_path(r'^api/billing-total-cost/$', views.TotalCustomerCostView.as_view()),
]
