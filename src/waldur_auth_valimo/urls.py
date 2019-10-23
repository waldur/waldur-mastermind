from . import views


def register_in(router):
    router.register(r'auth-valimo', views.AuthResultViewSet, basename='auth-valimo')
