import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from waldur_core.core.middleware import get_skip_side_effects

from .tasks import cleanup_actions_for_deleted_object

logger = logging.getLogger(__name__)


def schedule_cleanup_for_deleted_object(sender, instance, **kwargs):
    """
    Signal handler to schedule cleanup of user actions for a deleted object.

    Usage in app's signals/ready method:

    from django.db.models.signals import post_delete
    from waldur_core.user_actions.handlers import schedule_cleanup_for_deleted_object

    post_delete.connect(
        schedule_cleanup_for_deleted_object,
        sender=YourModel,
        dispatch_uid="cleanup_user_actions_for_your_model"
    )
    """
    if get_skip_side_effects():
        return
    # Best-effort: a failure here must never break the delete, and the
    # periodic cleanup_dangling_user_actions task catches what is missed.
    # The pk is read now because the deletion clears it on the instance.
    object_id = instance.pk
    try:
        content_type_id = ContentType.objects.get_for_model(sender).id
    except Exception:
        logger.exception(
            f"Failed to schedule cleanup for deleted {sender.__name__} object {object_id}"
        )
        return

    def schedule():
        try:
            cleanup_actions_for_deleted_object.delay(content_type_id, object_id)
            logger.debug(f"Scheduled cleanup for {sender.__name__} object {object_id}")
        except Exception:
            logger.exception(
                f"Failed to schedule cleanup for deleted {sender.__name__} object {object_id}"
            )

    # Queue only once the delete is committed: a rolled-back delete leaves the
    # object, and its actions, in place.
    transaction.on_commit(schedule, using=kwargs.get("using"))


def register_cleanup_handler(*model_classes):
    """
    Convenient helper to register cleanup handlers for multiple models.

    Usage:

    from waldur_core.user_actions.handlers import register_cleanup_handler
    from .models import Order, Offering, Resource

    register_cleanup_handler(Order, Offering, Resource)
    """
    from django.db.models.signals import post_delete

    for model_class in model_classes:
        post_delete.connect(
            schedule_cleanup_for_deleted_object,
            sender=model_class,
            dispatch_uid=f"cleanup_user_actions_for_{model_class._meta.label_lower}",
        )
