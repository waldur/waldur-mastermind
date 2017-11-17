from django.core.management.base import BaseCommand
from django.db import transaction

from waldur_mastermind.billing import models


class Command(BaseCommand):
    help = "Create or update price estimates based on invoices."

    def handle(self, *args, **options):
        with transaction.atomic():
            for model in models.PriceEstimate.get_estimated_models():
                for instance in model.objects.all():
                    estimate, _ = models.PriceEstimate.objects.get_or_create(scope=instance)
                    estimate.update_total()
                    estimate.save()
