import logging
from datetime import timedelta
from urllib.parse import urlparse

from celery import shared_task
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from waldur_core.core.models import User
from waldur_core.core.utils import chunks
from waldur_core.permissions.models import UserRole
from waldur_core.server.celery_settings import (
    DEFAULT_SCIM_RECONCILIATION_SCHEDULE_HOURS,
)
from waldur_core.structure import models as structure_models
from waldur_core.users.scim.client import ScimClient, ScimError
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OfferingUserStates

logger = logging.getLogger(__name__)

DEFAULT_SCIM_REQUEST_TIMEOUT = 10
DEFAULT_SCIM_BATCH_SIZE = 20


def get_project_content_type():
    return ContentType.objects.get_for_model(structure_models.Project)


def get_user_entitlements(data: dict) -> set[str]:
    entitlements = data.get("entitlements") or []
    result = set()
    for entry in entitlements:
        value = entry.get("value") if isinstance(entry, dict) else None
        if value:
            result.add(value)
    return result


def check_user_needs_entitlement(user: User) -> bool:
    project_ct = get_project_content_type()
    roles = UserRole.objects.filter(user=user, is_active=True, content_type=project_ct)
    return roles.exists()


def is_scim_configured() -> bool:
    return bool(
        config.SCIM_API_URL and config.SCIM_URN_NAMESPACE and config.SCIM_API_KEY
    )


def get_scim_client() -> ScimClient | None:
    if not config.SCIM_API_URL:
        logger.warning("SCIM_API_URL is not configured, skipping.")
        return None
    return ScimClient(
        config.SCIM_API_URL,
        api_key=config.SCIM_API_KEY,
        timeout=DEFAULT_SCIM_REQUEST_TIMEOUT,
    )


def get_scim_namespace() -> str | None:
    return config.SCIM_URN_NAMESPACE


def extract_hostname_from_ssh_url(url: str) -> str | None:
    """Extract hostname from ssh:// URL."""
    parsed = urlparse(url)
    if parsed.scheme.lower() == "ssh" and parsed.hostname:
        return parsed.hostname
    return None


def get_user_ssh_login_nodes(user: User):
    """
    Get SSH login nodes mapped to offering-specific usernames for a user.
    """
    project_ct = get_project_content_type()
    user_roles = UserRole.objects.filter(
        user=user, is_active=True, content_type=project_ct
    )
    project_ids = user_roles.values_list("object_id", flat=True).distinct()

    if not project_ids:
        return {}

    resources = marketplace_models.Resource.objects.filter(
        project_id__in=project_ids, state=marketplace_models.Resource.States.OK
    ).select_related("offering")

    offering_ids = resources.values_list("offering_id", flat=True).distinct()

    if not offering_ids:
        return {}

    # Get offering users that are in OK state and have a username
    offering_users = (
        marketplace_models.OfferingUser.objects.filter(
            user=user,
            offering_id__in=offering_ids,
            state=OfferingUserStates.OK,
        )
        .exclude(username__isnull=True)
        .exclude(username="")
        .select_related("offering")
    )

    if not offering_users:
        logger.warning(
            "SCIM user %s has no offering users in OK state, skipping.", user.uuid
        )
        return {}

    offering_user_username_map = {
        off_user.offering: off_user.username for off_user in offering_users
    }

    ssh_endpoints = marketplace_models.OfferingAccessEndpoint.objects.filter(
        offering__in=offering_user_username_map.keys(), url__startswith="ssh://"
    ).select_related("offering")

    login_node_username_map = {}
    for endpoint in ssh_endpoints:
        hostname = extract_hostname_from_ssh_url(endpoint.url)
        if hostname:
            offering_user_username = offering_user_username_map.get(endpoint.offering)
            if offering_user_username:
                login_node_username_map[hostname] = offering_user_username

    return login_node_username_map


def sync_user(user: User, client: ScimClient, urn_namespace: str) -> None:
    if not user.username:
        logger.warning("SCIM user %s has no username, skipping.", user.uuid)
        return

    login_node_username_map = get_user_ssh_login_nodes(user)
    should_have_entitlements = check_user_needs_entitlement(user) and user.is_active

    # Always check remote entitlements to ensure cleanup
    try:
        scim_user = client.get_user(user.username)
    except ScimError as exc:
        logger.warning("SCIM get_user failed for %s: %s", user.username, exc)
        return

    remote_entitlements = get_user_entitlements(scim_user)

    # If user has no login nodes with offering users, they shouldn't have entitlements
    if not login_node_username_map:
        if remote_entitlements:
            logger.info(
                "SCIM clear all entitlements for user %s (no login nodes with offering users)",
                user.username,
            )
            client.clear_all_entitlements(user.username)
        return

    # Skip if user shouldn't have anything nor has any remote entitlements
    if not should_have_entitlements and not remote_entitlements:
        return

    # Build expected entitlements using offering-specific usernames
    expected_entitlements = {
        client.build_entitlement(urn_namespace, login_node, offering_username)
        for login_node, offering_username in login_node_username_map.items()
    }

    try:
        if should_have_entitlements:
            # Find entitlements to remove (stale - no longer in expected)
            entitlements_to_remove = [
                entitlement
                for entitlement in remote_entitlements
                if entitlement not in expected_entitlements
            ]
            if entitlements_to_remove:
                logger.info(
                    "SCIM remove %d stale entitlements for user %s: %s",
                    len(entitlements_to_remove),
                    user.username,
                    entitlements_to_remove,
                )
                client.remove_entitlements(user.username, entitlements_to_remove)

            entitlements_to_add = [
                entitlement
                for entitlement in expected_entitlements
                if entitlement not in remote_entitlements
            ]
            if entitlements_to_add:
                logger.info(
                    "SCIM add %d entitlements for user %s: %s",
                    len(entitlements_to_add),
                    user.username,
                    entitlements_to_add,
                )
                client.add_entitlements(user.username, entitlements_to_add)
        else:
            # User shouldn't have entitlements - remove all entitlements
            if remote_entitlements:
                logger.info(
                    "SCIM clear all entitlements for user %s (inactive or no roles)",
                    user.username,
                )
                client.clear_all_entitlements(user.username)

    except ScimError as exc:
        logger.warning("SCIM update failed for %s: %s", user.username, exc)


def _get_reconciliation_cutoff():
    # Lookback window is 2x the schedule interval to ensure no missed updates
    lookback_hours = DEFAULT_SCIM_RECONCILIATION_SCHEDULE_HOURS * 2
    return timezone.now() - timedelta(hours=lookback_hours)


def _get_offering_user_ids_for_reconciliation(**filters):
    """Offering users tied to a reconcile candidate; sync_user applies add/remove/clear."""
    return (
        marketplace_models.OfferingUser.objects.filter(**filters)
        .values_list("user_id", flat=True)
        .distinct()
    )


def get_users_for_reconciliation():
    """Users with recent entitlement-relevant changes (grants and revocations).

    Selection is state-agnostic: active/inactive roles, any OfferingUser state,
    any Resource state. ``sync_user`` decides whether to add, remove, or clear
    remote entitlements.
    """
    cutoff = _get_reconciliation_cutoff()
    user_ids: set[int] = set()

    user_ids.update(
        UserRole.objects.filter(modified__gte=cutoff).values_list("user_id", flat=True)
    )

    user_ids.update(
        _get_offering_user_ids_for_reconciliation(modified__gte=cutoff),
    )

    offering_ids_from_resources = (
        marketplace_models.Resource.objects.filter(modified__gte=cutoff)
        .values_list("offering_id", flat=True)
        .distinct()
    )
    user_ids.update(
        _get_offering_user_ids_for_reconciliation(
            offering_id__in=offering_ids_from_resources
        ),
    )

    users = User.objects.filter(id__in=user_ids)
    logger.debug("SCIM reconcile users count=%s", users.count())
    return users


@shared_task(name="waldur_core.users.scim.sync_user_entitlements")
def sync_user_entitlements(user_uuid: str) -> None:
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED:
        return
    if not is_scim_configured():
        return
    try:
        user = User.all_objects.get(uuid=user_uuid)
        if not user.is_active:
            logger.warning("Warning, SCIM user %s is not active.", user.uuid)
    except User.DoesNotExist:
        return
    client = get_scim_client()
    urn_namespace = get_scim_namespace()
    if not client or not urn_namespace:
        return
    sync_user(user, client, urn_namespace)


@shared_task(name="waldur_core.users.scim.sync_user_batch_entitlements")
def sync_user_batch_entitlements(user_uuids: list[str]) -> None:
    """Process a batch of users for SCIM entitlement synchronization."""
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED:
        return
    if not is_scim_configured():
        return

    client = get_scim_client()
    urn_namespace = get_scim_namespace()
    if not client or not urn_namespace:
        return

    for user_uuid in user_uuids:
        try:
            user = User.all_objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            logger.warning("SCIM user %s not found, skipping.", user_uuid)
            continue
        sync_user(user, client, urn_namespace)


@shared_task(name="waldur_core.users.scim.sync_recent_entitlements")
def sync_recent_entitlements() -> None:
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED:
        return
    if not is_scim_configured():
        logger.warning("SCIM is enabled but not configured, skipping.")
        return

    users = get_users_for_reconciliation()
    user_uuids = [user.uuid.hex for user in users]
    total_users = len(user_uuids)
    logger.info(
        "SCIM reconcile: scheduling %d users in batches of %d",
        total_users,
        DEFAULT_SCIM_BATCH_SIZE,
    )

    if not user_uuids:
        return

    for batch_uuids in chunks(user_uuids, DEFAULT_SCIM_BATCH_SIZE):
        try:
            sync_user_batch_entitlements.delay(batch_uuids)
        except Exception as e:
            logger.error("Failed to schedule SCIM batch task: %s", e)


@shared_task(name="waldur_core.users.scim.sync_users_for_offering_endpoint")
def sync_users_for_offering_endpoint(offering_uuid: str) -> None:
    """Sync SCIM entitlements for all eligible users of an offering."""
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED:
        return
    if not is_scim_configured():
        return

    try:
        offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
    except marketplace_models.Offering.DoesNotExist:
        logger.warning("SCIM: offering %s not found, skipping.", offering_uuid)
        return

    offering_users = (
        marketplace_models.OfferingUser.objects.filter(
            offering=offering,
            state=OfferingUserStates.OK,
        )
        .exclude(username__isnull=True)
        .exclude(username="")
        .select_related("user")
    )

    user_uuids = [ou.user.uuid.hex for ou in offering_users]

    if not user_uuids:
        logger.info(
            "SCIM Offering Endpoint change: no eligible users for offering %s, nothing to sync.",
            offering_uuid,
        )
        return

    logger.info(
        "SCIM Offering Endpoint change: scheduling sync for %d users of offering %s in batches of %d",
        len(user_uuids),
        offering_uuid,
        DEFAULT_SCIM_BATCH_SIZE,
    )
    for batch in chunks(user_uuids, DEFAULT_SCIM_BATCH_SIZE):
        try:
            sync_user_batch_entitlements.delay(batch)
        except Exception as e:
            logger.error("Failed to schedule SCIM batch task: %s", e)


@shared_task(name="waldur_core.users.scim.sync_all_entitlements")
def sync_all_entitlements() -> None:
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED:
        return
    if not is_scim_configured():
        return

    user_ids = (
        UserRole.objects.filter(is_active=True)
        .values_list("user_id", flat=True)
        .distinct()
    )
    users = User.objects.filter(id__in=user_ids)
    user_uuids = [user.uuid.hex for user in users]
    total_users = len(user_uuids)
    logger.info(
        "SCIM full sync: scheduling %d users in batches of %d",
        total_users,
        DEFAULT_SCIM_BATCH_SIZE,
    )

    if not user_uuids:
        return

    for batch_uuids in chunks(user_uuids, DEFAULT_SCIM_BATCH_SIZE):
        try:
            sync_user_batch_entitlements.delay(batch_uuids)
        except Exception as e:
            logger.error("Failed to schedule SCIM batch task: %s", e)
