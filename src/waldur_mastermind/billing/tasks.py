from celery import shared_task

from . import models


@shared_task(name="waldur_mastermind.billing.refresh_estimates")
def refresh_estimates():
    for est in models.PriceEstimate.objects.all():
        est.update_total()
        est.save(update_fields=["total"])
