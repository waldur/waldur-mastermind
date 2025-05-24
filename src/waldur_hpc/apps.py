from django.apps import AppConfig
from django.db.models import signals


class HPCConfig(AppConfig):
    name = "waldur_hpc"

    def ready(self):
        from waldur_core.core.models import User

        from . import handlers

        signals.post_save.connect(
            handlers.handle_new_user,
            sender=User,
            dispatch_uid="waldur_hpc.handle_new_user",
        )
