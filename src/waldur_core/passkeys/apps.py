from django.apps import AppConfig


class PasskeysConfig(AppConfig):
    name = "waldur_core.passkeys"
    verbose_name = "Passkeys"

    def ready(self):
        from waldur_core.passkeys import checks  # noqa: F401
