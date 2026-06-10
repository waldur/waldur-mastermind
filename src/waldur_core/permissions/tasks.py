import logging

from celery import shared_task
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.models import User
from waldur_core.core.utils import chunked_queryset

from . import models
from .handlers import (
    deactivate_user_with_logging,
    reactivate_user_with_logging,
)
from .utils import exclude_removed_project_roles, get_scope_ancestors

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

    # Local import: marketplace depends on waldur_core, so a top-level
    # import here would form a circular dependency at app-load time.
    from waldur_mastermind.marketplace.enums import CourseAccountState
    from waldur_mastermind.marketplace.models import CourseAccount

    has_active_role = Exists(
        exclude_removed_project_roles(
            models.UserRole.objects.filter(user=OuterRef("pk"), is_active=True)
        )
    )
    has_ok_course_account = Exists(
        CourseAccount.objects.filter(
            user=OuterRef("pk"),
            state=CourseAccountState.OK,
            project__is_removed=False,
        )
    )

    # Push the per-user role/course-account checks into SQL via Exists()
    # subqueries so the DB returns only the (usually tiny) set of users
    # that actually need a state flip. The previous loop did N user-level
    # round-trips against UserRole and CourseAccount on every run.
    candidates = User.all_objects.filter(is_staff=False, is_support=False).annotate(
        has_active_role=has_active_role,
        has_ok_course_account=has_ok_course_account,
    )
    to_deactivate = candidates.filter(
        is_active=True, has_active_role=False, has_ok_course_account=False
    )
    # Administratively deactivated users are an explicit staff override and
    # must never be revived by the automatic sync, regardless of roles.
    to_reactivate = candidates.filter(
        is_active=False, is_admin_deactivated=False
    ).filter(Q(has_active_role=True) | Q(has_ok_course_account=True))

    deactivated_count = 0
    for user in chunked_queryset(to_deactivate, chunk_size=100, max_records=200_000):
        # Build the descriptive reason used by audit consumers. For users
        # that have at least one CourseAccount row (just not an OK one in
        # an active project), include the breakdown to make support
        # tickets easier to triage.
        ca_details = list(
            CourseAccount.objects.filter(user=user).values_list(
                "uuid", "state", "project__uuid", "project__is_removed"
            )
        )
        if ca_details:
            reason = (
                f"No active roles, {len(ca_details)} course account(s) "
                f"but none in OK state with active project. Details: {ca_details}"
            )
        else:
            reason = "No active roles and no course accounts"
        deactivate_user_with_logging(user, reason)
        deactivated_count += 1

    reactivated_count = 0
    for user in chunked_queryset(to_reactivate, chunk_size=100, max_records=200_000):
        reactivate_user_with_logging(user, "Periodic sync - has active roles")
        reactivated_count += 1

    logger.info(
        f"User deactivation sync completed. Deactivated: {deactivated_count}, Reactivated: {reactivated_count}"
    )


def _revoke_user_roles_outside_availability(role) -> int:
    """Revoke ``UserRole``s for ``role`` whose scope (and ancestors) is no
    longer covered by any RoleAvailability row. Returns the number of
    revocations performed. A role with zero RoleAvailability rows is
    treated as system-wide (per RoleAvailability docstring) — nothing is
    revoked in that case."""
    if not models.RoleAvailability.objects.filter(role=role).exists():
        return 0

    revoked = 0
    active_user_roles = models.UserRole.objects.filter(role=role, is_active=True)
    for user_role in chunked_queryset(
        active_user_roles, chunk_size=200, max_records=500_000
    ):
        scope = user_role.scope
        if scope is None:
            continue
        ancestors = get_scope_ancestors(scope)
        still_valid = any(
            models.RoleAvailability.objects.filter(
                role=role,
                content_type=ContentType.objects.get_for_model(ancestor),
                object_id=ancestor.id,
            ).exists()
            for ancestor in ancestors
        )
        if not still_valid:
            user_role.revoke(reason="Role availability removed")
            revoked += 1
    return revoked


@shared_task(name="waldur_core.permissions.reconcile_user_roles_for_role")
def reconcile_user_roles_for_role(role_id: int) -> None:
    """Async revocation triggered by RoleAvailability removal."""
    role = models.Role.objects.filter(id=role_id).first()
    if role is None:
        return
    revoked = _revoke_user_roles_outside_availability(role)
    if revoked:
        logger.info(
            "Reconciled UserRoles for role %s (id=%s): revoked %d row(s).",
            role.name,
            role.id,
            revoked,
        )


@shared_task(name="waldur_core.permissions.reconcile_user_roles_against_availability")
def reconcile_user_roles_against_availability() -> None:
    """Periodic safety net: reconcile UserRoles against RoleAvailability for
    every role that has at least one availability row.

    Catches drift from manual DB edits / failed signals.
    """
    role_ids = list(
        models.RoleAvailability.objects.values_list("role_id", flat=True).distinct()
    )
    total_revoked = 0
    for role in chunked_queryset(
        models.Role.objects.filter(id__in=role_ids),
        chunk_size=50,
        max_records=10_000,
    ):
        total_revoked += _revoke_user_roles_outside_availability(role)
    logger.info(
        "Periodic UserRole reconciliation: %d role(s) checked, %d revocation(s).",
        len(role_ids),
        total_revoked,
    )
