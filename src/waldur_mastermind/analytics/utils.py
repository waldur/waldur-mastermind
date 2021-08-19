import logging
from datetime import timedelta

from django.utils import timezone
from reversion.models import Version

from waldur_core.structure.models import Customer, Project

from . import models

logger = logging.getLogger(__name__)


def import_daily_usage():
    quotas = {}
    cutoff = timezone.now() - timedelta(days=90)
    versions = Version.objects.filter(revision__date_created__gte=cutoff).order_by(
        'revision__date_created'
    )
    for version in versions:
        try:
            scope = version.object.scope
        except AttributeError:
            continue
        if not isinstance(scope, (Customer, Project)):
            continue
        name = version.object.name
        usage = version._object_version.object.usage
        date = version.revision.date_created.date()

        quotas.setdefault(scope, {})
        quotas[scope].setdefault(name, {})
        quotas[scope][name][date] = usage

    end = timezone.now().date()
    for scope in quotas.keys():
        for name in quotas[scope].keys():
            records = quotas[scope][name]
            start = min(records.keys())
            days = (end - start).days
            usage = 0
            for i in range(days + 1):
                date = start + timedelta(days=i)
                usage = records.get(date, usage)
                models.DailyQuotaHistory.objects.update_or_create_quota(
                    scope, name, date, usage
                )
