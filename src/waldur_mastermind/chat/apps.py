from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "waldur_mastermind.chat"
    verbose_name = "Chat"

    def ready(self):
        """Import components and tools to register them with their registries."""
        from waldur_mastermind.chat import components  # noqa: F401
        from waldur_mastermind.chat.tools import (  # noqa: F401
            call_insights,
            create_vm,
            find_matching_calls,
            guide_proposal,
            list_projects,
            preview_vm,
            proposal_overview,
            review_assistant,
            review_workload,
            show_user_resources,
        )
