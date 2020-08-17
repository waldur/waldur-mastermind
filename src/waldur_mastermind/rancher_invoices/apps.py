from django.apps import AppConfig
from django.db.models import signals


class RancherInvoicesConfig(AppConfig):
    name = 'waldur_mastermind.rancher_invoices'
    verbose_name = 'Rancher'

    def ready(self):
        from waldur_rancher import models as rancher_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from . import handlers

        signals.post_save.connect(
            handlers.update_node_usage,
            sender=rancher_models.Node,
            dispatch_uid='support_invoices.handlers.update_node_usage',
        )

        signals.post_save.connect(
            handlers.create_invoice_item_if_component_usage_has_been_created,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid='support_invoices.handlers.create_invoice_item_if_component_usage_has_been_created',
        )
