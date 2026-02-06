import logging
from datetime import timedelta

from celery import shared_task
from constance import config
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from waldur_core.core import utils as core_utils

from . import models, providers

User = get_user_model()
logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.user_actions.update_user_actions")
def update_user_actions(provider_action_type=None, user_uuid=None):
    """Update actions for all providers or specific provider.

    If user_uuid is provided, only update actions for that specific user.
    """
    # Check if user actions system is enabled
    if not config.USER_ACTIONS_ENABLED:
        logger.info("User actions system is disabled, skipping action updates")
        return

    if provider_action_type:
        provider_classes = [provider_action_type]
    else:
        provider_classes = list(providers.get_all_providers().keys())

    logger.info(f"Updating user actions for providers: {provider_classes}")

    if user_uuid:
        try:
            user = User.objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            logger.error(f"User {user_uuid} not found")
            return
        for action_type in provider_classes:
            try:
                update_user_actions_for_provider.delay(user.id, action_type)
            except Exception as e:
                logger.error(
                    f"Failed to queue action update for user {user_uuid}, "
                    f"provider {action_type}: {e}"
                )
    else:
        for action_type in provider_classes:
            try:
                update_actions_for_provider.delay(action_type)
            except Exception as e:
                logger.error(f"Failed to queue action update for {action_type}: {e}")


@shared_task(name="waldur_core.user_actions.update_actions_for_provider")
def update_actions_for_provider(action_type):
    """Update actions for a specific provider"""
    provider = providers.get_provider(action_type)
    if not provider:
        logger.warning(f"Provider not found for action type: {action_type}")
        return

    logger.info(f"Updating actions for provider: {action_type}")

    try:
        # Update provider execution status
        provider_record, created = models.UserActionProvider.objects.get_or_create(
            action_type=action_type,
            defaults={
                "app_name": provider.__class__.__module__.split(".")[0],
                "provider_class": f"{provider.__class__.__module__}.{provider.__class__.__name__}",
            },
        )

        provider_record.update_execution_status("STARTED")

        # Get affected users
        users = provider.get_affected_users()
        logger.info(f"Found {len(users)} potentially affected users for {action_type}")

        success_count = 0
        error_count = 0

        for user in users:
            try:
                update_user_actions_for_provider.delay(user.id, action_type)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to queue user action update for {user}: {e}")
                error_count += 1

        status = f"COMPLETED: {success_count} queued, {error_count} errors"
        provider_record.update_execution_status(status)
        logger.info(f"Finished updating {action_type}: {status}")

    except Exception as e:
        logger.error(f"Error updating actions for provider {action_type}: {e}")
        if "provider_record" in locals():
            provider_record.update_execution_status(f"ERROR: {str(e)}")


@shared_task(name="waldur_core.user_actions.update_user_actions_for_provider")
def update_user_actions_for_provider(user_id, action_type):
    """Update actions for specific user and provider"""
    try:
        user = User.objects.get(id=user_id)
        provider = providers.get_provider(action_type)

        if not provider:
            logger.warning(f"Provider not found for action type: {action_type}")
            return

        # Get current actions from provider
        new_actions = provider.get_actions_for_user(user)

        logger.debug(
            f"Found {len(new_actions)} actions for user {user.username}, type {action_type}"
        )

        # Track created/updated actions
        created_count = 0
        updated_count = 0

        # Update or create actions
        for action_data in new_actions:
            related_object = action_data.pop("related_object")
            content_type = ContentType.objects.get_for_model(related_object)

            # Get corrective actions from provider
            corrective_actions = provider.get_corrective_actions(user, related_object)
            corrective_actions_data = [
                {
                    "label": ca.label,
                    "category": ca.category.value,
                    "severity": ca.severity.value,
                    "method": ca.method,
                    "api_endpoint": ca.api_endpoint,
                    "confirmation_required": ca.confirmation_required,
                    "permissions_required": ca.permissions_required,
                    "metadata": ca.metadata,
                    "route_name": ca.route_name,
                    "route_params": ca.route_params,
                }
                for ca in corrective_actions
            ]

            action, created = models.UserAction.objects.update_or_create(
                user=user,
                action_type=action_type,
                content_type=content_type,
                object_id=related_object.id,
                defaults={
                    **action_data,
                    "corrective_actions": corrective_actions_data,
                    "last_updated_by_task": f"update_user_actions_for_provider.{action_type}",
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        logger.debug(
            f"User {user.username}, type {action_type}: "
            f"created {created_count}, updated {updated_count}"
        )

        # Clean up stale actions
        cleanup_stale_actions.delay(user_id, action_type)

    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
    except Exception as e:
        logger.error(
            f"Failed to update actions for user {user_id}, type {action_type}: {e}"
        )


@shared_task(name="waldur_core.user_actions.cleanup_stale_actions")
def cleanup_stale_actions(user_id, action_type):
    """Remove actions that are no longer relevant"""
    try:
        user = User.objects.get(id=user_id)
        provider = providers.get_provider(action_type)

        if not provider:
            return

        # Get current valid actions from provider
        current_actions = provider.get_actions_for_user(user)

        # Extract (content_type_id, object_id) pairs for current valid actions
        current_object_refs = set()
        for action_data in current_actions:
            related_object = action_data["related_object"]
            content_type = ContentType.objects.get_for_model(related_object)
            current_object_refs.add((content_type.id, related_object.id))

        # Find actions in database that are no longer in current set
        existing_actions = models.UserAction.objects.filter(
            user=user, action_type=action_type
        )

        stale_count = 0
        for action in existing_actions:
            if (action.content_type_id, action.object_id) not in current_object_refs:
                action.delete()
                stale_count += 1

        if stale_count > 0:
            logger.debug(
                f"Cleaned up {stale_count} stale actions for user {user.username}, "
                f"type {action_type}"
            )

    except User.DoesNotExist:
        logger.error(f"User {user_id} not found during cleanup")
    except Exception as e:
        logger.error(
            f"Failed to cleanup stale actions for user {user_id}, type {action_type}: {e}"
        )


@shared_task(name="waldur_core.user_actions.cleanup_expired_silenced_actions")
def cleanup_expired_silenced_actions():
    """Remove or unsilence actions with expired temporary silence"""
    now = timezone.now()

    # Find actions with expired temporary silence
    expired_actions = models.UserAction.objects.filter(
        silenced_until__lt=now,
        silenced_until__isnull=False,
        is_silenced=False,  # Only temporarily silenced actions
    )

    count = expired_actions.count()
    if count > 0:
        # Clear the temporary silence
        expired_actions.update(silenced_until=None)
        logger.info(f"Unsilenced {count} actions with expired temporary silence")

    return count


@shared_task(name="waldur_core.user_actions.cleanup_old_action_executions")
def cleanup_old_action_executions(days_to_keep=None):
    """Clean up old action execution records"""
    if days_to_keep is None:
        days_to_keep = config.USER_ACTIONS_EXECUTION_RETENTION_DAYS
    cutoff_date = timezone.now() - timedelta(days=days_to_keep)

    old_executions = models.UserActionExecution.objects.filter(
        executed_at__lt=cutoff_date
    )

    count = old_executions.count()
    if count > 0:
        old_executions.delete()
        logger.info(
            f"Deleted {count} old action execution records older than {days_to_keep} days"
        )

    return count


@shared_task(name="waldur_core.user_actions.cleanup_actions_for_deleted_object")
def cleanup_actions_for_deleted_object(content_type_id, object_id):
    """Clean up user actions for a specific deleted object"""
    try:
        content_type = ContentType.objects.get(id=content_type_id)

        # Find and delete all actions related to this specific object
        actions = models.UserAction.objects.filter(
            content_type=content_type, object_id=object_id
        )

        count = actions.count()
        if count > 0:
            actions.delete()
            logger.info(
                f"Cleaned up {count} user actions for deleted {content_type.model} "
                f"object {object_id}"
            )

        return count

    except ContentType.DoesNotExist:
        logger.warning(f"ContentType {content_type_id} not found during cleanup")
        return 0
    except Exception as e:
        logger.error(f"Error cleaning up actions for deleted object: {e}")
        return 0


@shared_task(name="waldur_core.user_actions.cleanup_dangling_user_actions")
def cleanup_dangling_user_actions():
    """Clean up user actions pointing to non-existent objects (fallback periodic cleanup)"""
    deleted_count = 0

    # Get all user actions
    all_actions = models.UserAction.objects.select_related("content_type").all()

    for action in all_actions:
        try:
            # Try to access the related object to check if it exists
            if action.related_object is None:
                # Object doesn't exist anymore, delete the action
                action.delete()
                deleted_count += 1
                logger.debug(
                    f"Deleted dangling user action {action.id} for "
                    f"{action.content_type.model} object {action.object_id}"
                )
        except Exception as e:
            # Object doesn't exist or any other error, delete the action
            action.delete()
            deleted_count += 1
            logger.debug(
                f"Deleted user action {action.id} due to error accessing related object: {e}"
            )

    if deleted_count > 0:
        logger.info(f"Cleaned up {deleted_count} dangling user actions")

    return deleted_count


@shared_task(name="waldur_core.user_actions.send_user_action_notification")
def send_user_action_notification(user_uuid):
    """Send action digest notification to a specific user (staff-triggered)."""
    from django.db.models import Count

    try:
        user = (
            User.objects.filter(uuid=user_uuid)
            .filter(actions__is_silenced=False)
            .exclude(actions__silenced_until__gt=timezone.now())
            .annotate(action_count=Count("actions"))
            .get()
        )
    except User.DoesNotExist:
        logger.error(f"User {user_uuid} not found")
        return

    if not user.email:
        logger.warning(f"User {user.username} has no email address")
        return

    high_urgency_count = (
        user.actions.filter(urgency="high", is_silenced=False)
        .exclude(silenced_until__gt=timezone.now())
        .count()
    )

    context = {
        "user": user,
        "action_count": user.action_count,
        "high_urgency_count": high_urgency_count,
        "site_name": config.SITE_NAME,
        "actions_url": core_utils.format_homeport_link("profile/"),
    }

    core_utils.broadcast_mail(
        "user_actions", "notification_digest", context, [user.email]
    )
    logger.info(
        f"Sent action notification to {user.username}: "
        f"{user.action_count} total, {high_urgency_count} high urgency"
    )


@shared_task(name="waldur_core.user_actions.send_action_digest_notifications")
def send_action_digest_notifications():
    """Send daily digest notifications to users with pending actions"""
    # Check if user actions system is enabled
    if not config.USER_ACTIONS_ENABLED:
        logger.info("User actions system is disabled, skipping digest notifications")
        return 0

    from django.db.models import Count

    # Find users with active (non-silenced) actions
    users_with_actions = (
        User.objects.filter(actions__is_silenced=False)
        .exclude(actions__silenced_until__gt=timezone.now())
        .annotate(action_count=Count("actions"))
        .filter(action_count__gt=0)
    )

    notification_threshold = config.USER_ACTIONS_NOTIFICATION_THRESHOLD
    check_high_urgency = config.USER_ACTIONS_HIGH_URGENCY_NOTIFICATION

    digest_count = 0
    for user in users_with_actions:
        high_urgency_count = 0
        if check_high_urgency:
            high_urgency_count = (
                user.actions.filter(urgency="high", is_silenced=False)
                .exclude(silenced_until__gt=timezone.now())
                .count()
            )

        # Only send digest if user has high urgency actions or many actions
        should_notify = (check_high_urgency and high_urgency_count > 0) or (
            user.action_count > notification_threshold
        )

        if should_notify:
            # Send notification using broadcast_mail
            context = {
                "user": user,
                "action_count": user.action_count,
                "high_urgency_count": high_urgency_count,
                "site_name": config.SITE_NAME,
                "actions_url": core_utils.format_homeport_link("profile/"),
            }

            core_utils.broadcast_mail(
                "user_actions", "notification_digest", context, [user.email]
            )
            logger.info(
                f"Sent action digest to {user.username}: "
                f"{user.action_count} total, {high_urgency_count} high urgency"
            )
            digest_count += 1

    logger.info(f"Processed action digest notifications for {digest_count} users")
    return digest_count
