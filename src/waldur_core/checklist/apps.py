from django.apps import AppConfig


class ChecklistConfig(AppConfig):
    name = "waldur_core.checklist"
    verbose_name = "Checklist"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        pass
