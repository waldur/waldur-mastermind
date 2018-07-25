import itertools
from celery import shared_task

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from . import models


def get_user_allocations(user):
    project_permissions = structure_models.ProjectPermission.objects.filter(user=user, is_active=True)
    projects = project_permissions.values_list('project_id', flat=True)
    project_allocations = models.Allocation.objects.filter(is_active=True, service_project_link__project__in=projects)

    customer_permissions = structure_models.CustomerPermission.objects.filter(user=user, is_active=True)
    customers = customer_permissions.values_list('customer_id', flat=True)
    customer_allocations = models.Allocation.objects.filter(is_active=True, service_project_link__project__customer__in=customers)

    return itertools.chain(project_allocations, customer_allocations)


def get_structure_allocations(structure):
    if isinstance(structure, structure_models.Project):
        return list(models.Allocation.objects.filter(is_active=True, service_project_link__project=structure))
    elif isinstance(structure, structure_models.Customer):
        return list(models.Allocation.objects.filter(is_active=True, service_project_link__project__customer=structure))
    else:
        return []


@shared_task(name='waldur_slurm.add_user')
def add_user(serialized_profile):
    profile = core_utils.deserialize_instance(serialized_profile)
    for allocation in get_user_allocations(profile.user):
        allocation.get_backend().add_user(allocation, profile.username)


@shared_task(name='waldur_slurm.delete_user')
def delete_user(serialized_profile):
    profile = core_utils.deserialize_instance(serialized_profile)
    for allocation in get_user_allocations(profile.user):
        allocation.get_backend().delete_user(allocation, profile.username)


@shared_task(name='waldur_slurm.process_role_granted')
def process_role_granted(serialized_profile, serialized_structure):
    profile = core_utils.deserialize_instance(serialized_profile)
    structure = core_utils.deserialize_instance(serialized_structure)

    allocations = get_structure_allocations(structure)

    for allocation in allocations:
        allocation.get_backend().add_user(allocation, profile.username)


@shared_task(name='waldur_slurm.process_role_revoked')
def process_role_revoked(serialized_profile, serialized_structure):
    profile = core_utils.deserialize_instance(serialized_profile)
    structure = core_utils.deserialize_instance(serialized_structure)

    allocations = get_structure_allocations(structure)

    for allocation in allocations:
        allocation.get_backend().delete_user(allocation, profile.username)
