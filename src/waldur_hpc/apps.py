from django.apps import AppConfig
from django.contrib.auth import get_user_model
from django.db.models import signals


class HPCConfig(AppConfig):
    name = "waldur_hpc"

    def ready(self):
        from . import handlers

        User = get_user_model()

        signals.post_save.connect(
            handlers.handle_new_user,
            sender=User,
            dispatch_uid="waldur_hpc.handle_new_user",
        )
