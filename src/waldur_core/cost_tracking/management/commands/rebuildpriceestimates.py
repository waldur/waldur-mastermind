from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.cost_tracking import models, CostTrackingRegister, tasks


class Command(BaseCommand):
    help = ("Delete all price estimates that are related to current month and "
            "create new ones based on current consumption.")

    def handle(self, *args, **options):
        today = timezone.now()
        with transaction.atomic():
            # Delete current month price estimates
            models.PriceEstimate.objects.filter(month=today.month, year=today.year).delete()
            # Create new estimates for resources and ancestors
            for resource_model in CostTrackingRegister.registered_resources:
                for resource in resource_model.objects.all():
                    configuration = CostTrackingRegister.get_configuration(resource)
                    date = max(core_utils.month_start(today), resource.created)
                    models.PriceEstimate.create_historical(resource, configuration, date)
            # recalculate consumed estimate
            tasks.recalculate_estimate()
