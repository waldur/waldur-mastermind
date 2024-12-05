from django.apps import AppConfig
from django.db.models import signals


class EventsConfig(AppConfig):
    name = "waldur_core.logging"
    verbose_name = "Logging"

    def ready(self):
        from rest_framework.authtoken.models import Token

        from waldur_core.logging import handlers, models

        signals.post_save.connect(
            handlers.process_hook,
            sender=models.Event,
            dispatch_uid="waldur_core.logging.handlers.process_hook",
        )

        signals.post_delete.connect(
            handlers.delete_stale_event_subscriptions,
            sender=Token,
            dispatch_uid="waldur_core.logging.handlers.delete_stale_event_subscriptions",
        )
