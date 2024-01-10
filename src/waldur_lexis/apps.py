from django.apps import AppConfig
from django.db.models import signals


class LexisConfig(AppConfig):
    name = "waldur_lexis"
    verbose_name = "LEXIS"
    service_name = "LEXIS"

    def ready(self) -> None:
        from waldur_lexis import handlers
        from waldur_mastermind.marketplace import models as marketplace_models

        signals.post_save.connect(
            handlers.request_ssh_key_for_heappe_robot_account,
            sender=marketplace_models.RobotAccount,
            dispatch_uid="waldur_lexis.handlers.request_ssh_key_for_heappe_robot_account",
        )
