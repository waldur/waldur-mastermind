from waldur_mastermind.invoices import views


def register_in(router):
    router.register(r'invoices', views.InvoiceViewSet, basename='invoice')
    router.register(
        r'payment-profiles', views.PaymentProfileViewSet, basename='payment-profile',
    )
    router.register(
        r'payments', views.PaymentViewSet, basename='payment',
    )
