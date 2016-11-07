from django.utils import timezone

from nodeconductor.core import utils as core_utils


def get_current_month():
    return timezone.now().month


def get_current_year():
    return timezone.now().year


def get_current_month_end():
    return core_utils.month_end(timezone.now())


def get_current_month_start():
    return core_utils.month_start(timezone.now())
