from . import views


def register_in(router):
    router.register(
        r'marketplace-slurm-remote',
        views.SlurmViewSet,
        basename='marketplace-slurm-remote',
    )
