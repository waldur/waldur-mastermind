from django.apps import AppConfig
from django.db.models import signals


class EventsConfig(AppConfig):
    name = 'waldur_core.logging'
    verbose_name = 'Logging'

    def ready(self):
        from waldur_core.logging import handlers, models

        signals.post_save.connect(
            handlers.process_hook,
            sender=models.Event,
            dispatch_uid='waldur_core.logging.handlers.process_hook',
        )
