from . import views


def register_in(router):
    router.register(r'slurm', views.SlurmServiceViewSet, basename='slurm')
    router.register(r'slurm-service-project-link', views.SlurmServiceProjectLinkViewSet,
                    basename='slurm-spl')
    router.register(r'slurm-allocation', views.AllocationViewSet, basename='slurm-allocation')
    router.register(r'slurm-allocation-usage', views.AllocationUsageViewSet, basename='slurm-allocation-usage')
