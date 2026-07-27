"""
Utility functions for user data access logging.
"""

import logging

from constance import config
from django.db import transaction

from waldur_core.core import utils as core_utils

logger = logging.getLogger(__name__)


def get_client_ip(request):
    """Canonical client IP address for the audit record, or None.

    Resolution and normalisation are delegated to the platform-wide helpers so
    this log spells an address the same way every other audit surface does. A
    mixed-case IPv6 literal, its canonical form and its IPv4-mapped wrapper are
    three different strings for one client, which would split an accessor
    across rows that "every access from X" cannot join.
    """
    if request is None:
        return None

    raw = core_utils.get_ip_address(request)
    address = core_utils.normalize_ip_address(raw)
    if raw and address is None:
        # Dropping it silently would hide a misconfigured proxy: the column is
        # nullable, so an unparseable value is indistinguishable from no header.
        logger.warning("Invalid IP address in request: %s", raw)
    return address


def determine_accessor_type(accessor, target_user):
    """
    Determine the type of accessor based on user properties.

    Args:
        accessor: The user accessing the data
        target_user: The user whose data is being accessed

    Returns:
        str: One of 'self', 'staff', 'support', 'organization_member', 'service_provider'
    """
    if accessor == target_user:
        return "self"
    if accessor.is_staff:
        return "staff"
    if accessor.is_support:
        return "support"
    # Default to organization member for other users who have permission
    return "organization_member"


def should_log_user_data_access(request, target_user, accessor):
    """
    Determine whether a user data access should be logged.

    Args:
        request: The HTTP request
        target_user: The user whose data is being accessed
        accessor: The user accessing the data

    Returns:
        bool: True if the access should be logged
    """
    # Check if logging is enabled
    if not config.USER_DATA_ACCESS_LOGGING_ENABLED:
        return False

    # Check if self-access should be logged
    if accessor == target_user and not config.USER_DATA_ACCESS_LOG_SELF_ACCESS:
        return False

    return True


def log_user_data_access_sync(
    target_user, accessor, request, accessed_fields, context=None
):
    """
    Log user data access synchronously.

    This creates a UserDataAccessLog entry directly in the database.
    Synchronous logging is preferred over async (Celery) because:
    - It's a simple, fast DB INSERT operation
    - GDPR compliance requires reliable logging
    - No dependency on Celery worker being available

    Args:
        target_user: The user whose data was accessed
        accessor: The user who accessed the data
        request: The HTTP request
        accessed_fields: List of field names that were accessed
        context: Additional context dict (optional)
    """
    from waldur_core.logging.models import UserDataAccessLog

    if not should_log_user_data_access(request, target_user, accessor):
        return

    accessor_type = determine_accessor_type(accessor, target_user)
    ip_address = get_client_ip(request)

    # Build context
    log_context = context or {}
    if request:
        log_context["endpoint"] = request.path
        log_context["method"] = request.method

    try:
        with transaction.atomic():
            UserDataAccessLog.objects.create(
                target_user=target_user,
                accessor=accessor,
                accessor_type=accessor_type,
                accessed_fields=accessed_fields,
                ip_address=ip_address,
                context=log_context,
            )
    except Exception as e:
        # Log error but don't fail the request.
        # The savepoint (transaction.atomic) ensures a failed INSERT
        # does not corrupt the outer transaction.
        logger.warning("Failed to log user data access: %s", e)


def bulk_log_user_data_access(entries, accessor, request):
    """Bulk log user data access entries using a single INSERT.

    Used by list views to avoid N+1 INSERT queries when serializing
    multiple users. Also avoids N+1 constance config lookups by
    checking settings once before the bulk operation.
    """
    from waldur_core.logging.models import UserDataAccessLog

    if not entries:
        return

    if not config.USER_DATA_ACCESS_LOGGING_ENABLED:
        return

    log_self_access = config.USER_DATA_ACCESS_LOG_SELF_ACCESS
    ip_address = get_client_ip(request)
    log_context = {}
    if request:
        log_context["endpoint"] = request.path
        log_context["method"] = request.method

    logs = []
    for entry in entries:
        target_user = entry["target_user"]
        if accessor == target_user and not log_self_access:
            continue
        accessor_type = determine_accessor_type(accessor, target_user)
        logs.append(
            UserDataAccessLog(
                target_user=target_user,
                accessor=accessor,
                accessor_type=accessor_type,
                accessed_fields=entry["accessed_fields"],
                ip_address=ip_address,
                context=log_context,
            )
        )

    if logs:
        try:
            with transaction.atomic():
                UserDataAccessLog.objects.bulk_create(logs)
        except Exception as e:
            logger.warning("Failed to bulk log user data access: %s", e)


# Backwards compatibility alias
log_user_data_access_async = log_user_data_access_sync


def log_user_data_access_on_commit(
    target_user, accessor, request, accessed_fields, context=None
):
    """
    Log user data access after database commit.

    This ensures the log is only created after the current transaction commits,
    preventing issues if the transaction is rolled back.

    Args:
        target_user: The user whose data was accessed
        accessor: The user who accessed the data
        request: The HTTP request
        accessed_fields: List of field names that were accessed
        context: Additional context dict (optional)
    """
    transaction.on_commit(
        lambda: log_user_data_access_sync(
            target_user, accessor, request, accessed_fields, context
        )
    )
