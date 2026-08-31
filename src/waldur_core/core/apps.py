from constance import signals as constance_signals
from django.apps import AppConfig
from django.db.models import signals
from django_fsm import signals as fsm_signals


class CoreConfig(AppConfig):
    name = "waldur_core.core"
    verbose_name = "Core"

    def ready(self):
        from health_check.plugins import plugin_dir
        from rest_framework.authtoken.models import Token

        import waldur_core.core.openapi_extensions  # noqa
        from waldur_core.core import (
            checks,  # noqa
            db_template_cache,
            handlers,
        )
        from waldur_core.core.health_checks import CeleryWorkersHealthCheck
        from waldur_core.core.models import StateMixin, User

        SshPublicKey = self.get_model("SshPublicKey")
        NotificationTemplate = self.get_model("NotificationTemplate")

        signals.pre_save.connect(
            handlers.preserve_fields_before_update,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.preserve_fields_before_update",
        )

        signals.post_save.connect(
            handlers.create_auth_token,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.create_auth_token",
        )

        signals.post_save.connect(
            handlers.log_user_save,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.log_user_save",
        )

        signals.post_save.connect(
            handlers.set_default_token_lifetime,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.set_default_token_lifetime",
        )

        signals.post_delete.connect(
            handlers.log_user_delete,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.log_user_delete",
        )

        signals.post_save.connect(
            handlers.log_ssh_key_save,
            sender=SshPublicKey,
            dispatch_uid="waldur_core.core.handlers.log_ssh_key_save",
        )

        for model in (User, SshPublicKey, NotificationTemplate):
            signals.post_save.connect(
                handlers.create_initial_revision,
                sender=model,
                dispatch_uid=f"waldur_core.core.create_initial_revision_{model.__name__}",
            )

        signals.post_save.connect(
            db_template_cache.add_template_to_cache,
            sender=NotificationTemplate,
            dispatch_uid="waldur_core.core.db_template_cache.add_template_to_cache",
        )

        signals.pre_delete.connect(
            db_template_cache.remove_cached_template,
            sender=NotificationTemplate,
            dispatch_uid="waldur_core.core.db_template_cache.remove_cached_template",
        )

        signals.post_save.connect(
            handlers.create_revision_on_update,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.create_revision_on_update",
        )

        signals.post_delete.connect(
            handlers.log_ssh_key_delete,
            sender=SshPublicKey,
            dispatch_uid="waldur_core.core.handlers.log_ssh_key_delete",
        )

        signals.post_save.connect(
            handlers.log_token_create,
            sender=Token,
            dispatch_uid="waldur_core.core.handlers.log_token_create",
        )

        signals.pre_save.connect(
            handlers.revoke_user_pats_on_deactivation,
            sender=User,
            dispatch_uid="waldur_core.core.handlers.revoke_user_pats_on_deactivation",
        )

        from axes.signals import user_locked_out

        user_locked_out.connect(
            handlers.log_user_locked_out,
            dispatch_uid="waldur_core.core.handlers.log_user_locked_out",
        )

        constance_signals.config_updated.connect(handlers.constance_updated)

        for index, model in enumerate(StateMixin.get_all_models()):
            fsm_signals.post_transition.connect(
                handlers.delete_error_message,
                sender=model,
                dispatch_uid=f"waldur_core.core.handlers.delete_error_message_{model.__name__}_{index}",
            )

        # Register custom Celery health check
        plugin_dir.register(CeleryWorkersHealthCheck)
