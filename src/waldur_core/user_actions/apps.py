import importlib
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class UserActionsConfig(AppConfig):
    name = "waldur_core.user_actions"
    verbose_name = "User Actions"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Auto-register providers on app startup
        self.register_providers()

    def register_providers(self):
        """Auto-discover and register action providers from all apps"""
        from django.apps import apps

        for app_config in apps.get_app_configs():
            try:
                # Try to import user_actions module from each app
                module_name = f"{app_config.name}.user_actions"
                importlib.import_module(module_name)
                logger.debug(f"Loaded user action providers from {module_name}")
            except ImportError:
                # Module doesn't exist, which is fine
                continue
            except Exception as e:
                logger.warning(
                    f"Error loading user action providers from {app_config.name}: {e}"
                )
                continue
