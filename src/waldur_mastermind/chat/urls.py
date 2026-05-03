from waldur_mastermind.chat import views
from waldur_mastermind.chat.anonymous import views as anonymous_views


def register_in(router):
    router.register(
        r"chat",
        views.ChatViewSet,
        basename="chat",
    )
    router.register(
        r"marketplace-chat",
        anonymous_views.MarketplaceChatViewSet,
        basename="marketplace-chat",
    )
    router.register(
        r"anonymous-chat-interactions",
        anonymous_views.AnonymousChatInteractionViewSet,
        basename="anonymous-chat-interaction",
    )
    router.register(
        r"anonymous-chat-feedbacks",
        anonymous_views.AnonymousChatFeedbackViewSet,
        basename="anonymous-chat-feedback",
    )
    router.register(
        r"chat-quota",
        views.TokenQuotaViewSet,
        basename="chatquota",
    )
    router.register(
        r"chat-tools",
        views.ToolViewSet,
        basename="chat-tools",
    )
    router.register(
        r"chat-sessions",
        views.ChatSessionViewSet,
        basename="chat-session",
    )
    router.register(
        r"chat-threads",
        views.ThreadSessionViewSet,
        basename="chat-thread",
    )
    router.register(
        r"chat-messages",
        views.MessageViewSet,
        basename="chat-message",
    )
    router.register(
        r"chat-system-prompts",
        views.SystemPromptViewSet,
        basename="chat-system-prompt",
    )
