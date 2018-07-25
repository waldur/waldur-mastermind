from django.contrib.admin.models import LogEntry
from django.core.management.base import BaseCommand

from waldur_core.cost_tracking import models as cost_tracking_models


class Command(BaseCommand):
    help = "Remove instances that have FK to stale content types."

    def handle(self, *args, **options):
        for estimate in cost_tracking_models.PriceEstimate.objects.all():
            if estimate.content_type.model_class() is None:
                estimate.delete()

        for item in cost_tracking_models.DefaultPriceListItem.objects.all():
            if item.resource_content_type.model_class() is None:
                item.delete()

        for entry in LogEntry.objects.all():
            if entry.content_type.model_class() is None:
                entry.delete()
