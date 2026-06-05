from django.apps import AppConfig


class MatrixChatConfig(AppConfig):
    name = "waldur_mastermind.matrix_chat"
    verbose_name = "Matrix Chat"

    def ready(self):
        from django.db.models import signals

        from waldur_core.permissions import signals as permission_signals
        from waldur_core.structure.models import Project
        from waldur_mastermind.marketplace.models import Order

        from . import handlers

        permission_signals.role_granted.connect(
            handlers.on_role_granted,
            dispatch_uid="waldur_mastermind.matrix_chat.on_role_granted",
        )

        permission_signals.role_revoked.connect(
            handlers.on_role_revoked,
            dispatch_uid="waldur_mastermind.matrix_chat.on_role_revoked",
        )

        signals.pre_delete.connect(
            handlers.on_project_pre_delete,
            sender=Project,
            dispatch_uid="waldur_mastermind.matrix_chat.on_project_pre_delete",
        )

        signals.post_save.connect(
            handlers.on_order_state_changed,
            sender=Order,
            dispatch_uid="waldur_mastermind.matrix_chat.on_order_state_changed",
        )
