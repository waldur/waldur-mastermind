from . import views


def register_in(router):
    router.register(r'broadcast_messages', views.BroadcastMessageViewSet)
    router.register(r'broadcast_message_templates', views.MessageTemplateViewSet)
