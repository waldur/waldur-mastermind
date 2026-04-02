import logging

from celery import shared_task
from constance import config
from django.utils import timezone

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.models import User

from . import models
from .handlers import (
    deactivate_user_with_logging,
    reactivate_user_with_logging,
    should_deactivate_user,
    should_reactivate_user,
)

logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.permissions.check_expired_permissions")
def check_expired_permissions():
    for permission in models.UserRole.objects.filter(
        expiration_time__lt=timezone.now(), is_active=True
    ):
        permission.revoke(reason="Automatic expiration cleanup task")


@shared_task(name="waldur_core.permissions.sync_user_deactivation_status")
def sync_user_deactivation_status():
    """
    Sync user activation status based on DEACTIVATE_USER_IF_NO_ROLES setting.

    This task ensures all users in the system match the current policy:
    - If DEACTIVATE_USER_IF_NO_ROLES is True: deactivate users with no active roles
    - If DEACTIVATE_USER_IF_NO_ROLES is False: reactivate users who were previously auto-deactivated
    """
    if not config.DEACTIVATE_USER_IF_NO_ROLES:
        # If setting is disabled, don't perform any deactivation logic
        logger.debug(
            "DEACTIVATE_USER_IF_NO_ROLES is disabled, skipping user deactivation sync"
        )
        return

    if get_skip_side_effects():
        # Skip during import operations to avoid interfering with bulk data imports
        logger.debug(
            "Side effects are disabled (likely during import), skipping user deactivation sync"
        )
        return

    deactivated_count = 0
    reactivated_count = 0

    # Process all non-staff/non-support users with iterator for memory efficiency
    # Use all_objects to include inactive users (needed for reactivation checks)
    for user in User.all_objects.filter(is_staff=False, is_support=False).iterator(
        chunk_size=100
    ):
        if should_deactivate_user(user):
            deactivate_user_with_logging(user, "Periodic sync - no active roles")
            deactivated_count += 1
        elif should_reactivate_user(user):
            reactivate_user_with_logging(user, "Periodic sync - has active roles")
            reactivated_count += 1

    logger.info(
        f"User deactivation sync completed. Deactivated: {deactivated_count}, Reactivated: {reactivated_count}"
    )
