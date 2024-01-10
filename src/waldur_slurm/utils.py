import itertools
import re

from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_slurm import models

MAPPING = {
    "cpu_usage": "nc_cpu_usage",
    "gpu_usage": "nc_gpu_usage",
    "ram_usage": "nc_ram_usage",
}

FIELD_NAMES = MAPPING.keys()

QUOTA_NAMES = MAPPING.values()


def format_current_month():
    today = timezone.now()
    month_start = core_utils.month_start(today).strftime("%Y-%m-%d")
    month_end = core_utils.month_end(today).strftime("%Y-%m-%d")
    return month_start, month_end


def sanitize_allocation_name(name):
    incorrect_symbols_regex = r"[^%s]+" % models.SLURM_ALLOCATION_REGEX
    return re.sub(incorrect_symbols_regex, "", name)


def get_user_allocations(user):
    connected_projects = get_connected_projects(user)
    connected_customers = get_connected_customers(user)

    project_allocations = models.Allocation.objects.filter(
        is_active=True, project__in=connected_projects
    )

    customer_allocations = models.Allocation.objects.filter(
        is_active=True, project__customer__in=connected_customers
    )

    return (project_allocations, customer_allocations)


def get_profile_allocations(profile):
    return itertools.chain(*get_user_allocations(profile.user))
