from . import views


def register_in(router):
    router.register(
        r'marketplace-slurm',
        views.SlurmViewSet,
        basename='marketplace-slurm',
    )
