from . import views


def register_in(router):
    router.register(r'slurm', views.SlurmServiceViewSet, basename='slurm')
    router.register(
        r'slurm-service-project-link',
        views.SlurmServiceProjectLinkViewSet,
        basename='slurm-spl',
    )
    router.register(
        r'slurm-allocations', views.AllocationViewSet, basename='slurm-allocation'
    )
    router.register(
        r'slurm-allocation-user-usage',
        views.AllocationUserUsageViewSet,
        basename='slurm-allocation-user-usage',
    )
    router.register(
        r'slurm-associations', views.AssociationViewSet, basename='slurm-association',
    )
