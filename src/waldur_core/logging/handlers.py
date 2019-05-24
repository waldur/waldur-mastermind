from django.contrib.contenttypes import models as ct_models
from django.db import transaction

from waldur_core.logging import models, tasks


def remove_related_alerts(sender, instance, **kwargs):
    content_type = ct_models.ContentType.objects.get_for_model(instance)
    for alert in models.Alert.objects.filter(
            object_id=instance.id, content_type=content_type, closed__isnull=True).iterator():
        alert.close()


def process_hook(sender, instance, created=False, **kwargs):
    transaction.on_commit(lambda: tasks.process_event.delay(instance.pk))
