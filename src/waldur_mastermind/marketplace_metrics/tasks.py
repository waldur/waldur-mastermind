from celery import shared_task

from . import models, retention, rollups


@shared_task(name="waldur_mastermind.marketplace_metrics.roll_up")
def roll_up():
    """Recompute the roll-up buckets of points received since the last run."""
    rollups.roll_up()


@shared_task(name="waldur_mastermind.marketplace_metrics.roll_up_window")
def roll_up_window():
    """Recompute the whole window, in case a run left a bucket behind."""
    rollups.roll_up(whole_window=True)


@shared_task(name="waldur_mastermind.marketplace_metrics.enforce_retention")
def enforce_retention():
    retention.enforce()


@shared_task(name="waldur_mastermind.marketplace_metrics.purge_archived")
def purge_archived():
    retention.purge_archived()


@shared_task(name="waldur_mastermind.marketplace_metrics.purge_offering_metric")
def purge_offering_metric(uuid):
    offering_metric_id = (
        models.OfferingMetric.objects.filter(uuid=uuid)
        .values_list("id", flat=True)
        .first()
    )
    if offering_metric_id is not None:
        retention.purge(offering_metric_id)
