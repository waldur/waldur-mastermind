from celery import shared_task

from . import signals


@shared_task(name='waldur_core.quotas.update_custom_quotas')
def update_custom_quotas():
    signals.recalculate_quotas.send(sender=None)
