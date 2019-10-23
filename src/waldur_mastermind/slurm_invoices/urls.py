from waldur_mastermind.slurm_invoices import views


def register_in(router):
    router.register(r'slurm-packages', views.SlurmPackageViewSet, basename='slurm-package')
