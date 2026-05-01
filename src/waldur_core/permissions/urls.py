from . import views


def register_in(router):
    router.register(
        r"roles",
        views.RoleViewSet,
        basename="role",
    )
    router.register(
        r"role-availabilities",
        views.RoleAvailabilityViewSet,
        basename="role-availability",
    )
    router.register(r"user-permissions", views.UserPermissionViewSet)
