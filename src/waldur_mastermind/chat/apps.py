from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "waldur_mastermind.chat"
    verbose_name = "Chat"

    def ready(self):
        """Import components to register them with the registry"""
        from waldur_mastermind.chat import components  # noqa: F401
