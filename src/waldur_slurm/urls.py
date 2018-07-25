from . import views


def register_in(router):
    router.register(r'slurm', views.SlurmServiceViewSet, base_name='slurm')
    router.register(r'slurm-service-project-link', views.SlurmServiceProjectLinkViewSet,
                    base_name='slurm-spl')
    router.register(r'slurm-allocation', views.AllocationViewSet, base_name='slurm-allocation')
    router.register(r'slurm-allocation-usage', views.AllocationUsageViewSet, base_name='slurm-allocation-usage')
