from waldur_mastermind.invoices import views


def register_in(router):
    router.register(r'invoices', views.InvoiceViewSet, basename='invoice')
