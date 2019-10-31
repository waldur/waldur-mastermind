from waldur_core.users import views


def register_in(router):
    router.register(r'user-invitations', views.InvitationViewSet, basename='user-invitation')
