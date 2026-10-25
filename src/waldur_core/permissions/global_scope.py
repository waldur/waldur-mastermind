"""Whether a site-wide (global) grant applies, and to whom, at a known cost.

Every permission check consults the global scope, so its cost is what every
request pays. Two layers keep that cost at zero for a deployment that never
grants a global role, and at one query per request for one that does:

- A per-process flag remembers whether the site scope exists at all. The
  scope row is created on the first grant (``get_global_role_scope``), so until
  then nothing needs to be looked up. A save in this process sets the flag at
  once. Another process notices within ``SCOPE_RECHECK_SECONDS``, and until it
  does it treats the grant as absent: the first grant on a deployment may take
  that long to apply everywhere, and it never applies early.
- Within one request, a user's global permissions are loaded once and kept for
  the rest of the request. Outside a request (Celery, the shell) nothing is
  kept and each check asks the database.

A global grant never satisfies a permission in
:data:`enums.GLOBAL_DENIED_PERMISSIONS`, whatever the role carries.
"""

import threading
import time

from django.contrib.contenttypes.models import ContentType

from . import enums, models

SCOPE_RECHECK_SECONDS = 60

_now = time.monotonic
_lock = threading.Lock()
_scope = {"exists": False, "checked_at": None}
_request = threading.local()


def global_content_type_id() -> int:
    return ContentType.objects.get_by_natural_key(*enums.TYPE_MAP["global"]).id


def scope_may_exist() -> bool:
    """False only when this process has recently seen no site scope."""
    checked_at = _scope["checked_at"]
    if checked_at is not None and _now() - checked_at < SCOPE_RECHECK_SECONDS:
        return _scope["exists"]
    with _lock:
        _scope["exists"] = models.GlobalRoleScope.objects.exists()
        _scope["checked_at"] = _now()
        return _scope["exists"]


def mark_scope_exists() -> None:
    _scope["exists"] = True
    _scope["checked_at"] = _now()


def start_request(**kwargs) -> None:
    _request.permissions = {}


def end_request(**kwargs) -> None:
    _request.permissions = None


def forget_user(user_id) -> None:
    memo = getattr(_request, "permissions", None)
    if memo is not None:
        memo.pop(user_id, None)


def reset() -> None:
    """Forget everything cached in this process. For tests."""
    _scope["exists"] = False
    _scope["checked_at"] = None
    end_request()


def sync() -> None:
    """Set the flag from the database now. For tests.

    A test rolls back the site scope it created, but the flag that row set
    outlives it, so the next test would pay a lookup it does not need and count
    one query more than it would alone. Syncing before each test makes query
    counts independent of test order. Without database access the flag is left
    unknown and the first check looks.
    """
    try:
        exists = models.GlobalRoleScope.objects.exists()
    except Exception:  # noqa: BLE001 -- no database for this test
        reset()
        return
    _scope["exists"] = exists
    _scope["checked_at"] = _now()
    end_request()


def global_permissions(user) -> frozenset:
    """Permissions ``user`` holds on the site scope that a global grant may use."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return frozenset()
    if not scope_may_exist():
        return frozenset()
    memo = getattr(_request, "permissions", None)
    if memo is not None and user.pk in memo:
        return memo[user.pk]
    held = frozenset(
        models.UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type_id=global_content_type_id(),
            role__permissions__permission__isnull=False,
        ).values_list("role__permissions__permission", flat=True)
    )
    held -= enums.GLOBAL_DENIED_PERMISSIONS
    if memo is not None:
        memo[user.pk] = held
    return held


def on_scope_saved(sender, instance, **kwargs) -> None:
    mark_scope_exists()


def on_user_role_changed(sender, instance, **kwargs) -> None:
    if instance.content_type_id == global_content_type_id():
        mark_scope_exists()
        forget_user(instance.user_id)
