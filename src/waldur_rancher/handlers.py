import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.core.models import StateMixin

from . import models, tasks

logger = logging.getLogger(__name__)


def notify_create_user(sender, instance, password, created=False, **kwargs):
    transaction.on_commit(
        lambda: tasks.notify_create_user.delay(
            instance.id, password, instance.settings.backend_url
        )
    )


def delete_node_if_related_instance_has_been_deleted(sender, instance, **kwargs):
    try:
        content_type = ContentType.objects.get_for_model(instance)
        node = models.Node.objects.get(object_id=instance.id, content_type=content_type)
        backend = node.cluster.get_backend()
        backend.delete_node(node)
    except ObjectDoesNotExist:
        pass


def delete_cluster_if_all_related_nodes_have_been_deleted(sender, instance, **kwargs):
    node = instance
    try:
        if (
            node.cluster.state == models.Cluster.States.DELETING
            and not node.cluster.node_set.count()
        ):
            backend = node.cluster.get_backend()
            backend.delete_cluster(node.cluster)
    except models.Cluster.DoesNotExist:
        logger.warning('Cluster instance has been removed already.')


def set_error_state_for_node_if_related_instance_deleting_is_failed(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    try:
        content_type = ContentType.objects.get_for_model(instance)
        node = models.Node.objects.get(object_id=instance.id, content_type=content_type)
    except ObjectDoesNotExist:
        return

    if (
        instance.tracker.has_changed('state')
        and instance.state == StateMixin.States.ERRED
    ):
        node.state = models.Node.States.ERRED
        node.error_message = 'Deleting related VM has failed.'
        node.save()


def set_error_state_for_cluster_if_related_node_deleting_is_failed(
    sender, instance, created=False, **kwargs
):
    node = instance

    if created:
        return

    if node.tracker.has_changed('state') and node.state == models.Node.States.ERRED:
        if node.cluster.state == models.Cluster.States.DELETING:
            node.cluster.state = models.Cluster.States.ERRED
            node.cluster.error_message = 'Deleting one or a more nodes have failed.'
            node.cluster.save()


def delete_catalog_if_scope_has_been_deleted(sender, instance, **kwargs):
    content_type = ContentType.objects.get_for_model(instance)
    models.Catalog.objects.filter(
        object_id=instance.id, content_type=content_type
    ).delete()
