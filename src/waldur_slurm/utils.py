from django.utils import timezone

from waldur_core.core import utils as core_utils

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
