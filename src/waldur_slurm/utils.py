import itertools
import re

from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_slurm import models

MAPPING = {
    'cpu_usage': 'nc_cpu_usage',
    'gpu_usage': 'nc_gpu_usage',
    'ram_usage': 'nc_ram_usage',
}

FIELD_NAMES = MAPPING.keys()

QUOTA_NAMES = MAPPING.values()


def format_current_month():
    today = timezone.now()
    month_start = core_utils.month_start(today).strftime('%Y-%m-%d')
    month_end = core_utils.month_end(today).strftime('%Y-%m-%d')
    return month_start, month_end


def sanitize_allocation_name(name):
    incorrect_symbols_regex = r'[^%s]+' % models.SLURM_ALLOCATION_REGEX
    return re.sub(incorrect_symbols_regex, '', name)


def get_user_allocations(user):
    project_permissions = structure_models.ProjectPermission.objects.filter(
        user=user, is_active=True
    )
    projects = project_permissions.values_list('project_id', flat=True)
    project_allocations = models.Allocation.objects.filter(
        is_active=True, project__in=projects
    )

    customer_permissions = structure_models.CustomerPermission.objects.filter(
        user=user, is_active=True
    )
    customers = customer_permissions.values_list('customer_id', flat=True)
    customer_allocations = models.Allocation.objects.filter(
        is_active=True, project__customer__in=customers
    )

    return (project_allocations, customer_allocations)


def get_profile_allocations(profile):
    return itertools.chain(*get_user_allocations(profile.user))
