from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "waldur_mastermind.chat"
    verbose_name = "Chat"

    def ready(self):
        """Import components and tools to register them with their registries."""
        from waldur_mastermind.chat import components  # noqa: F401
        from waldur_mastermind.chat.tools import (  # noqa: F401
            account,
            ask_user,
            marketplace,
            proposals_researcher,
            proposals_reviewer,
            search_tools,
            vm,
        )
