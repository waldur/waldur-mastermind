from django.apps import AppConfig
from django.conf import settings


class MatrixChatConfig(AppConfig):
    name = "waldur_mastermind.matrix_chat"
    verbose_name = "Matrix Chat"

    def ready(self):
        from django.db.models import signals

        from waldur_core.core.models import User
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.permissions.models import RolePermission
        from waldur_core.permissions.pat_filtering import register_pat_filter
        from waldur_core.structure.models import Project
        from waldur_mastermind.marketplace.models import Order

        from . import handlers, managers, models

        permission_signals.role_granted.connect(
            handlers.on_role_granted,
            dispatch_uid="waldur_mastermind.matrix_chat.on_role_granted",
        )

        permission_signals.role_revoked.connect(
            handlers.on_role_revoked,
            dispatch_uid="waldur_mastermind.matrix_chat.on_role_revoked",
        )

        signals.post_save.connect(
            handlers.on_room_permission_changed,
            sender=RolePermission,
            dispatch_uid="waldur_mastermind.matrix_chat.on_room_permission_added",
        )

        signals.post_delete.connect(
            handlers.on_room_permission_changed,
            sender=RolePermission,
            dispatch_uid="waldur_mastermind.matrix_chat.on_room_permission_removed",
        )

        signals.post_save.connect(
            handlers.on_project_created,
            sender=Project,
            dispatch_uid="waldur_mastermind.matrix_chat.on_project_created",
        )

        signals.pre_delete.connect(
            handlers.on_project_pre_delete,
            sender=Project,
            dispatch_uid="waldur_mastermind.matrix_chat.on_project_pre_delete",
        )

        signals.post_save.connect(
            handlers.on_user_deactivated,
            sender=User,
            dispatch_uid="waldur_mastermind.matrix_chat.on_user_deactivated",
        )

        signals.post_save.connect(
            handlers.on_user_reactivated,
            sender=User,
            dispatch_uid="waldur_mastermind.matrix_chat.on_user_reactivated",
        )

        signals.post_save.connect(
            handlers.on_user_demoted,
            sender=User,
            dispatch_uid="waldur_mastermind.matrix_chat.on_user_demoted",
        )

        signals.pre_delete.connect(
            handlers.on_user_pre_delete,
            sender=User,
            dispatch_uid="waldur_mastermind.matrix_chat.on_user_pre_delete",
        )

        signals.post_save.connect(
            handlers.on_order_state_changed,
            sender=Order,
            dispatch_uid="waldur_mastermind.matrix_chat.on_order_state_changed",
        )

        signals.post_delete.connect(
            handlers.on_history_export_deleted,
            sender=models.MatrixHistoryExport,
            dispatch_uid="waldur_mastermind.matrix_chat.on_history_export_deleted",
        )

        register_pat_filter(models.MatrixHistoryExport)(managers.pat_filter_exports)

        if "corsheaders" in settings.INSTALLED_APPS:
            # Deployments that add django-cors-headers answer preflights
            # before cors_middleware does; let them allow the call token API
            # from any origin too.
            from corsheaders.signals import check_request_enabled

            check_request_enabled.connect(
                handlers.allow_public_cors,
                dispatch_uid="waldur_mastermind.matrix_chat.allow_public_cors",
            )
