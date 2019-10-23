from django.conf.urls import url

from . import views


def register_in(router):
    router.register(r'paypal-payments', views.PaymentView, basename='paypal-payment')
    router.register(r'paypal-invoices', views.InvoicesViewSet, basename='paypal-invoice')


urlpatterns = [
    url(r'^api/paypal-invoices-webhook/$', views.InvoiceWebHookViewSet.as_view(), name='paypal-invoice-webhook'),
]
