from . import views


def register_in(router):
    router.register(
        r"roles",
        views.RoleViewSet,
        basename="role",
    )
