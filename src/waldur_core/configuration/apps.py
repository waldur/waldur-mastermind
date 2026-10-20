from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ConfigurationConfig(AppConfig):
    name = "waldur_core.configuration"
    verbose_name = "Configuration"

    def ready(self):
        from waldur_core.configuration.models import mark_table_exists

        post_migrate.connect(
            mark_table_exists,
            sender=self,
            dispatch_uid="waldur_core.configuration.mark_table_exists",
        )
