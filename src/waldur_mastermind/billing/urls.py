from django.urls import re_path

from waldur_mastermind.billing import views


def register_in(router):
    router.register(
        r'financial-reports', views.FinancialReportView, basename='financial_report'
    )


urlpatterns = [
    re_path(r'^api/billing-total-cost/$', views.TotalCustomerCostView.as_view()),
]
