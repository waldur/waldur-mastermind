from django.apps import AppConfig
from django.db.models import signals


class AutoprovisioningConfig(AppConfig):
    name = "waldur_autoprovisioning"

    def ready(self):
        from waldur_core.core.models import User

        from . import handlers

        signals.post_save.connect(
            handlers.handle_new_user,
            sender=User,
            dispatch_uid="waldur_autoprovisioning.handle_new_user",
        )
