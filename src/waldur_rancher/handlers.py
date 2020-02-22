import logging

from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.core.models import StateMixin
from waldur_mastermind.common import utils as common_utils

from . import tasks, models, views

logger = logging.getLogger(__name__)


def notify_create_user(sender, instance, password, created=False, **kwargs):
    transaction.on_commit(lambda: tasks.notify_create_user.delay(instance.id,
                                                                 password,
                                                                 instance.settings.backend_url))


def delete_catalog_when_cluster_is_deleted(sender, instance, **kwargs):
    content_type = ContentType.objects.get_for_model(instance)
    models.Catalog.objects.filter(content_type=content_type, object_id=instance.id).delete()


def delete_node_if_related_instance_has_been_deleted(sender, instance, **kwargs):
    try:
        content_type = ContentType.objects.get_for_model(instance)
        node = models.Node.objects.get(
            object_id=instance.id,
            content_type=content_type,
            state=models.Node.States.DELETING
        )
        backend = node.cluster.get_backend()
        backend.delete_node(node)
        node.delete()
    except ObjectDoesNotExist:
        pass


def delete_cluster_if_all_related_nodes_have_been_deleted(sender, instance, **kwargs):
    node = instance

    if node.cluster.state == models.Cluster.States.DELETING and not node.cluster.node_set.count():
        backend = node.cluster.get_backend()
        backend.delete_cluster(node.cluster)
        node.cluster.delete()


def set_error_state_for_node_if_related_instance_deleting_is_failed(sender, instance, created=False, **kwargs):
    if created:
        return

    try:
        content_type = ContentType.objects.get_for_model(instance)
        node = models.Node.objects.get(object_id=instance.id, content_type=content_type)
    except ObjectDoesNotExist:
        return

    if instance.tracker.has_changed('state') and instance.state == StateMixin.States.ERRED:
        node.state = models.Node.States.ERRED
        node.error_message = 'Deleting related VM has failed.'
        node.save()


def set_error_state_for_cluster_if_related_node_deleting_is_failed(sender, instance, created=False, **kwargs):
    node = instance

    if created:
        return

    if node.tracker.has_changed('state') and node.state == models.Node.States.ERRED:
        if node.cluster.state == models.Cluster.States.DELETING:
            node.cluster.state = models.Cluster.States.ERRED
            node.cluster.error_message = 'Deleting one or a more nodes have failed.'
            node.cluster.save()


def retry_create_node_if_related_instance_has_been_deleted(sender, instance, **kwargs):
    try:
        content_type = ContentType.objects.get_for_model(instance)
        node = models.Node.objects.get(
            object_id=instance.id,
            content_type=content_type,
            state=models.Node.States.UPDATING
        )

        with transaction.atomic():
            node.delete()
            post_data = node.initial_data['rest_initial_data']
            user_id = node.initial_data['rest_user_id']
            user = auth.get_user_model().objects.get(pk=user_id)
            view = views.NodeViewSet.as_view({'post': 'create'})
            common_utils.create_request(view, user, post_data)
            backend = node.cluster.get_backend()
            backend.delete_node(node)
    except ObjectDoesNotExist:
        pass
