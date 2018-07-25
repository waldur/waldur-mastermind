from __future__ import unicode_literals

from django.apps import AppConfig
from django.contrib.auth import get_user_model
from django.db.models import signals
from django_fsm import signals as fsm_signals

from waldur_core.core.monkeypatch import monkey_patch_fields


class CoreConfig(AppConfig):
    name = 'waldur_core.core'
    verbose_name = 'Core'

    def ready(self):
        from waldur_core.core import handlers
        from waldur_core.core.models import StateMixin
        from rest_framework.authtoken.models import Token

        User = get_user_model()
        SshPublicKey = self.get_model('SshPublicKey')

        signals.pre_save.connect(
            handlers.preserve_fields_before_update,
            sender=User,
            dispatch_uid='waldur_core.core.handlers.preserve_fields_before_update',
        )

        signals.post_save.connect(
            handlers.create_auth_token,
            sender=User,
            dispatch_uid='waldur_core.core.handlers.create_auth_token',
        )

        signals.post_save.connect(
            handlers.log_user_save,
            sender=User,
            dispatch_uid='waldur_core.core.handlers.log_user_save',
        )

        signals.post_save.connect(
            handlers.set_default_token_lifetime,
            sender=User,
            dispatch_uid='waldur_core.core.handlers.set_default_token_lifetime',
        )

        signals.post_delete.connect(
            handlers.log_user_delete,
            sender=User,
            dispatch_uid='waldur_core.core.handlers.log_user_delete',
        )

        signals.post_save.connect(
            handlers.log_ssh_key_save,
            sender=SshPublicKey,
            dispatch_uid='waldur_core.core.handlers.log_ssh_key_save',
        )

        signals.post_delete.connect(
            handlers.log_ssh_key_delete,
            sender=SshPublicKey,
            dispatch_uid='waldur_core.core.handlers.log_ssh_key_delete',
        )

        signals.post_save.connect(
            handlers.log_token_create,
            sender=Token,
            dispatch_uid='waldur_core.core.handlers.log_token_create',
        )

        for index, model in enumerate(StateMixin.get_all_models()):
            fsm_signals.post_transition.connect(
                handlers.delete_error_message,
                sender=model,
                dispatch_uid='waldur_core.core.handlers.delete_error_message_%s_%s' % (model.__name__, index),
            )

        # Database fields should be patched only after database models are initialized
        monkey_patch_fields()
