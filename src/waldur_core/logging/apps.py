from django.apps import AppConfig
from django.db.models import signals
from django.utils.module_loading import autodiscover_modules


class EventsConfig(AppConfig):
    name = "waldur_core.logging"
    verbose_name = "Logging"

    def ready(self):
        from rest_framework.authtoken.models import Token

        from waldur_core.logging import handlers, models
        from waldur_core.logging.event_logger import event_emitted

        autodiscover_modules("log")
        event_emitted.connect(
            handlers.process_hook,
            dispatch_uid="waldur_core.logging.handlers.process_hook",
        )

        signals.post_delete.connect(
            handlers.delete_stale_event_subscriptions,
            sender=Token,
            dispatch_uid="waldur_core.logging.handlers.delete_stale_event_subscriptions",
        )

        signals.pre_delete.connect(
            handlers.cleanup_rabbitmq_queue_on_delete,
            sender=models.EventSubscriptionQueue,
            dispatch_uid="waldur_core.logging.handlers.cleanup_rabbitmq_queue_on_delete",
        )
