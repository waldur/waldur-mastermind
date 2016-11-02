from django.apps import AppConfig

from django.db.models import signals


class InvoiceConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.invoices'
    verbose_name = 'Waldur assembly Invoices'

    def ready(self):
        from . import handlers, models

        signals.post_save.connect(
            handlers.add_openstack_packages_details_to_new_invoice,
            sender=models.Invoice,
            dispatch_uid='nodeconductor_assembly_waldur.invoices.add_openstack_packages_details_to_new_invoice'
        )
