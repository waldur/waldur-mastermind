from . import views


def register_in(router):
    router.register(
        r'remote-waldur-api', views.RemoteWaldurViewSet, basename='remote-waldur-api'
    )
    router.register(
        r'remote-waldur', views.RemoteWaldurServiceViewSet, basename='remote-waldur'
    )
    router.register(
        r'remote-waldur-spl',
        views.ServiceProjectLinkViewSet,
        basename='remote-waldur-spl',
    )
