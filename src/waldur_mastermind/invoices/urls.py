from django.urls import re_path

from waldur_mastermind.invoices import views


def register_in(router):
    router.register(r'invoices', views.InvoiceViewSet, basename='invoice')
    router.register(r'invoice-items', views.InvoiceItemViewSet, basename='invoice-item')
    router.register(
        r'payment-profiles',
        views.PaymentProfileViewSet,
        basename='payment-profile',
    )
    router.register(
        r'payments',
        views.PaymentViewSet,
        basename='payment',
    )


urlpatterns = [
    re_path(
        r'^api/invoice/send-financial-report-by-mail/',
        views.send_financial_report_by_mail,
        name='send-financial-report-by-mail',
    ),
]
