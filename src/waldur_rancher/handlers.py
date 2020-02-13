import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from . import models, tasks

logger = logging.getLogger(__name__)


def notify_create_user(sender, instance, password, created=False, **kwargs):
    transaction.on_commit(lambda: tasks.notify_create_user.delay(instance.id,
                                                                 password,
                                                                 instance.settings.backend_url))


def delete_catalog_when_cluster_is_deleted(sender, instance, **kwargs):
    content_type = ContentType.objects.get_for_model(instance)
    models.Catalog.objects.filter(content_type=content_type, object_id=instance.id).delete()
