import logging

from django.contrib.contenttypes.models import ContentType

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
    try:
        content_type = ContentType.objects.get_for_model(sender)
        cleanup_actions_for_deleted_object.delay(content_type.id, instance.pk)
        logger.debug(f"Scheduled cleanup for {sender.__name__} object {instance.pk}")
    except Exception as e:
        logger.error(
            f"Failed to schedule cleanup for deleted {sender.__name__} object {instance.pk}: {e}"
        )


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
        logger.debug(f"Registered cleanup handler for {model_class._meta.label}")
