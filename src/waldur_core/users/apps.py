from django.apps import AppConfig
from django.db.models import signals


class UserConfig(AppConfig):
    name = 'waldur_core.users'
    verbose_name = 'Users'

    def ready(self):
        from waldur_core.users import handlers
        from waldur_core.users.models import PermissionRequest

        signals.post_save.connect(
            handlers.create_notification_about_permission_request_has_been_submitted,
            sender=PermissionRequest,
            dispatch_uid='waldur_core.users.handlers.create_notification_about_permission_request_has_been_submited',
        )
