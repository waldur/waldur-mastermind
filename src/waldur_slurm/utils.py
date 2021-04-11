import re

from django.utils import timezone

from waldur_core.core import utils as core_utils
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
