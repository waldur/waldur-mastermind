from __future__ import unicode_literals

from waldur_mastermind.slurm_invoices import views


def register_in(router):
    router.register(r'slurm-packages', views.SlurmPackageViewSet, base_name='slurm-package')
