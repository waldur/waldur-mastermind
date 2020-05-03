from waldur_mastermind.marketplace import processors
from waldur_slurm import views as slurm_views


class CreateAllocationProcessor(processors.BaseCreateResourceProcessor):
    viewset = slurm_views.AllocationViewSet

    fields = (
        'name',
        'description',
    )


class DeleteAllocationProcessor(processors.DeleteResourceProcessor):
    viewset = slurm_views.AllocationViewSet
