from . import views


def register_in(router):
    router.register(r"broadcast-messages", views.BroadcastMessageViewSet)
    router.register(r"broadcast-message-templates", views.MessageTemplateViewSet)
