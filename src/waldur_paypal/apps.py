from django.apps import AppConfig
from django.contrib.auth import get_user_model
from django.db.models import signals


class PayPalConfig(AppConfig):
    name = 'waldur_paypal'
    verbose_name = 'PayPal'

    def ready(self):
        from . import handlers
        from waldur_core.cost_tracking import signals as cost_signals

        Invoice = self.get_model('Invoice')
        User = get_user_model()

        signals.post_save.connect(
            handlers.log_invoice_save,
            sender=Invoice,
            dispatch_uid='waldur_paypal.handlers.log_invoice_save',
        )

        signals.post_delete.connect(
            handlers.log_invoice_delete,
            sender=Invoice,
            dispatch_uid='waldur_paypal.handlers.log_invoice_delete',
        )

        signals.post_save.connect(
            handlers.add_email_hooks_to_user,
            sender=User,
            dispatch_uid='waldur_paypal.handlers.add_email_hooks_to_user',
        )

        cost_signals.invoice_created.connect(
            handlers.create_invoice,
            sender=None,
            dispatch_uid='waldur_paypal.handlers.create_invoice',
        )
