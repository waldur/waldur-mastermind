from . import views


def register_in(router):
    router.register(
        r"hpc-workspaces", views.HpcWorkspaceViewSet, basename="hpc-workspace"
    )
