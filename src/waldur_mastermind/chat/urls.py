from waldur_mastermind.chat import views


def register_in(router):
    router.register(
        r"chat",
        views.ChatViewSet,
        basename="chat",
    )
