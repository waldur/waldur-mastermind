from django.apps import AppConfig
from django.db.models import signals


class LexisConfig(AppConfig):
    name = "waldur_lexis"
    verbose_name = "LEXIS"
    service_name = "LEXIS"

    def ready(self) -> None:
        from waldur_core.permissions.enums import PermissionEnum
        from waldur_lexis import handlers
        from waldur_lexis.exceptions import HeappeConfigError
        from waldur_lexis.models import get_heappe_config
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager

        def get_available_resource_actions(resource: marketplace_models.Resource):
            try:
                get_heappe_config(resource.offering)
            except HeappeConfigError:
                return []
            else:
                return [PermissionEnum.CREATE_LEXIS_LINK]

        signals.post_save.connect(
            handlers.request_ssh_key_for_heappe_robot_account,
            sender=marketplace_models.RobotAccount,
            dispatch_uid="waldur_lexis.handlers.request_ssh_key_for_heappe_robot_account",
        )

        manager.register(
            offering_type=self.service_name,
            get_available_resource_actions=get_available_resource_actions,
        )
