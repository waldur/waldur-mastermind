from waldur_mastermind.chat import views


def register_in(router):
    router.register(
        r"chat",
        views.ChatViewSet,
        basename="chat",
    )
    router.register(
        r"chat-tools",
        views.ToolViewSet,
        basename="chat-tools",
    )
