from celery import shared_task

from waldur_core.core.utils import chunked_queryset
from waldur_core.quotas import signals
from waldur_core.quotas.utils import get_models_with_quotas


@shared_task(name="waldur_core.quotas.update_custom_quotas")
def update_custom_quotas():
    signals.recalculate_quotas.send(sender=None)


@shared_task(name="waldur_core.quotas.update_standard_quotas")
def update_standard_quotas():
    for model in get_models_with_quotas():
        for field in model.get_quotas_fields():
            for instance in chunked_queryset(
                model.objects.all(), chunk_size=100, max_records=100_000
            ):
                field.recalculate(scope=instance)
