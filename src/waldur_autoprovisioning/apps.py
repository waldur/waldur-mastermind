from django.apps import AppConfig
from django.db.models import signals


class AutoprovisioningConfig(AppConfig):
    name = "waldur_autoprovisioning"

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.core.models import User

        from . import handlers

        signals.post_save.connect(
            handlers.handle_new_user,
            sender=User,
            dispatch_uid="waldur_autoprovisioning.handle_new_user",
        )

        # Re-evaluate the roles rules assert whenever identity data is refreshed
        # (OIDC login, SCIM pull). post_save is not enough and not right: it also
        # fires for ordinary profile edits, and it fires before the claims of a
        # login have necessarily been written.
        core_signals.user_identity_synced.connect(
            handlers.handle_identity_synced,
            dispatch_uid="waldur_autoprovisioning.handle_identity_synced",
        )
