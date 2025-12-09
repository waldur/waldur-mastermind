import logging

from django.contrib.contenttypes.models import ContentType

from . import models

logger = logging.getLogger(__name__)


def cleanup_actions_on_object_delete(sender, instance, **kwargs):
    """Clean up user actions when the related object is deleted"""
    try:
        content_type = ContentType.objects.get_for_model(sender)

        # Find and delete all actions related to this object
        actions = models.UserAction.objects.filter(
            content_type=content_type, object_id=instance.pk
        )

        count = actions.count()
        if count > 0:
            actions.delete()
            logger.debug(
                f"Cleaned up {count} user actions for deleted {sender.__name__} "
                f"object {instance.pk}"
            )

    except Exception as e:
        logger.error(f"Error cleaning up user actions for deleted object: {e}")


def update_actions_on_object_save(sender, instance, created, **kwargs):
    """Trigger action updates when related objects are saved"""
    # This could be used to trigger immediate action updates
    # when certain objects change, rather than waiting for periodic tasks
    pass
