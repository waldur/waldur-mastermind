from django.apps import AppConfig
from django.db.models import signals


class AuthSocialConfig(AppConfig):
    name = "waldur_auth_social"
    verbose_name = "Auth Social"

    def ready(self):
        from waldur_core.core.models import User

        from . import handlers

        signals.pre_save.connect(
            handlers.forget_oauth_tokens_on_deactivation,
            sender=User,
            dispatch_uid="waldur_auth_social.handlers.forget_oauth_tokens_on_deactivation",
        )
        signals.pre_delete.connect(
            handlers.forget_oauth_tokens_on_deletion,
            sender=User,
            dispatch_uid="waldur_auth_social.handlers.forget_oauth_tokens_on_deletion",
        )
