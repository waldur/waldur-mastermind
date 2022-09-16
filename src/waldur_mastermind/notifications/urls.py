from . import views


def register_in(router):
    router.register(r'notifications', views.BroadcastMessageViewSet)
