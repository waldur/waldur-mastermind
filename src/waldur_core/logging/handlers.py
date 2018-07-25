from django.contrib.contenttypes import models as ct_models

from waldur_core.logging import models


def remove_related_alerts(sender, instance, **kwargs):
    content_type = ct_models.ContentType.objects.get_for_model(instance)
    for alert in models.Alert.objects.filter(
            object_id=instance.id, content_type=content_type, closed__isnull=True).iterator():
        alert.close()
