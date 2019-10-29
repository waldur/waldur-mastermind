from . import views


def register_in(router):
    router.register(r'freeipa-profiles', views.ProfileViewSet, basename='freeipa-profile')
