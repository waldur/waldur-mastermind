from django.apps import AppConfig
from django.db.models import signals


class AuthSocialConfig(AppConfig):
    name = "waldur_auth_social"
    verbose_name = "Auth Social"

    def ready(self):
        from waldur_core.core import auth_hooks
        from waldur_core.core.enums import ProviderChoices
        from waldur_core.core.models import User

        from . import handlers, utils

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

        auth_hooks.register_logout_hook(handlers.forget_oauth_tokens_on_logout)
        auth_hooks.register_oidc_logout_url_provider(handlers.get_oidc_logout_url)
        auth_hooks.register_default_idp_provider(handlers.get_default_idp_info)
        auth_hooks.register_remote_user_puller(
            ProviderChoices.EDUTEAMS, utils.pull_remote_eduteams_user
        )
