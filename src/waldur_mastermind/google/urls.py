from . import views


def register_in(router):
    router.register(r'google-auth', views.GoogleAuthViewSet, basename='google-auth')
    router.register(
        r'google_credentials',
        views.GoogleCredentialsViewSet,
        basename='google_credential',
    )
