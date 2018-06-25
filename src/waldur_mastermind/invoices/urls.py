from __future__ import unicode_literals

from waldur_mastermind.invoices import views


def register_in(router):
    router.register(r'invoices', views.InvoiceViewSet, base_name='invoice')
