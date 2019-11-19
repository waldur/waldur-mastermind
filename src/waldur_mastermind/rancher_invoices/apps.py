from django.apps import AppConfig
from django.db.models import signals


class RancherInvoicesConfig(AppConfig):
    name = 'waldur_mastermind.rancher_invoices'
    verbose_name = 'Rancher'

    def ready(self):
        from waldur_rancher import models as rancher_models, signals as rancher_signals
        from waldur_mastermind.marketplace import models as marketplace_models
        from . import handlers

        rancher_signals.node_states_have_been_updated.connect(
            handlers.update_node_usage,
            sender=rancher_models.Cluster,
            dispatch_uid='support_invoices.handlers.update_node_usage',
        )

        signals.post_save.connect(
            handlers.create_invoice_item_if_component_usage_has_been_created,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid='support_invoices.handlers.create_invoice_item_if_component_usage_has_been_created',
        )
