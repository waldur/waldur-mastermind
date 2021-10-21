from waldur_core.users import views


def register_in(router):
    router.register(
        r'user-invitations', views.InvitationViewSet, basename='user-invitation'
    )
    router.register(
        r'user-group-invitations',
        views.GroupInvitationViewSet,
        basename='user-group-invitation',
    )

    router.register(
        r'user-permission-requests',
        views.PermissionRequestViewSet,
        basename='user-permission-request',
    )
