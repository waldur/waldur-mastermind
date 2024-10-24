from celery import shared_task

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure.exceptions import ServiceBackendNotImplemented

from . import backend, models, utils


def get_structure_allocations(structure):
    if isinstance(structure, structure_models.Project):
        return list(models.Allocation.objects.filter(is_active=True, project=structure))
    elif isinstance(structure, structure_models.Customer):
        return list(
            models.Allocation.objects.filter(
                is_active=True, project__customer=structure
            )
        )
    else:
        return []


@shared_task(name="waldur_slurm.add_user")
def add_user(serialized_profile):
    profile = core_utils.deserialize_instance(serialized_profile)
    for allocation in utils.get_profile_allocations(profile):
        allocation.get_backend().add_user(allocation, profile.user, profile.username)


@shared_task(name="waldur_slurm.delete_user")
def delete_user(serialized_profile):
    profile = core_utils.deserialize_instance(serialized_profile)
    for allocation in utils.get_profile_allocations(profile):
        allocation.get_backend().delete_user(allocation, profile.user, profile.username)


@shared_task(name="waldur_slurm.process_role_granted")
def process_role_granted(serialized_profile, serialized_structure):
    profile = core_utils.deserialize_instance(serialized_profile)
    structure = core_utils.deserialize_instance(serialized_structure)

    allocations = get_structure_allocations(structure)

    for allocation in allocations:
        try:
            allocation.get_backend().add_user(
                allocation, profile.user, profile.username
            )
        except ServiceBackendNotImplemented:
            # Ignore a case of the remote SLURM allocation
            pass


@shared_task(name="waldur_slurm.process_role_revoked")
def process_role_revoked(serialized_profile, serialized_structure):
    profile = core_utils.deserialize_instance(serialized_profile)
    structure = core_utils.deserialize_instance(serialized_structure)

    allocations = get_structure_allocations(structure)

    for allocation in allocations:
        allocation.get_backend().delete_user(allocation, profile.user, profile.username)


@shared_task(name="waldur_slurm.sync_allocation_users")
def sync_allocation_users(serialized_allocation):
    allocation = core_utils.deserialize_instance(serialized_allocation)
    slurm_backend: backend.SlurmBackend = allocation.get_backend()
    slurm_backend.sync_users(allocation)
