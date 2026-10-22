from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import sync_to_async
from django.db import connections
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from rest_framework.exceptions import AuthenticationFailed

from waldur_core.core import models
from waldur_core.core import utils as core_utils
from waldur_core.core.authentication import (
    PATAuthentication,
    authenticate_personal_access_token,
)
from waldur_core.permissions.enums import PermissionEnum


def drop_broken_connections():
    # Django drops broken connections only around its own requests, so this
    # long-lived process does it itself; otherwise a database restart would fail
    # every later call. close_old_connections() would also close healthy ones,
    # since CONN_MAX_AGE is 0, and reconnect on every call.
    for connection in connections.all(initialized_only=True):
        if connection.connection is not None and not connection.is_usable():
            connection.close()


def run_orm(func, *args):
    """Call an ORM function on a live connection."""
    drop_broken_connections()
    return func(*args)


# Off asgiref's single shared thread, so one slow query doesn't hold up every
# client's authentication. Tests turn it on to reach their transaction.
ORM_THREAD_SENSITIVE = False

# Each thread keeps its database connection between calls, so the pool size is
# the number of connections this process holds. asyncio's default executor
# would grow to min(32, cpu + 4) threads, counting the node's CPUs inside a pod.
DEFAULT_ORM_THREADS = 4
orm_executor = ThreadPoolExecutor(
    max_workers=DEFAULT_ORM_THREADS, thread_name_prefix="mcp-orm"
)


def configure_orm_threads(count: int) -> None:
    global orm_executor
    orm_executor.shutdown(wait=False)
    orm_executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix="mcp-orm")


async def run_orm_async(func, *args):
    if ORM_THREAD_SENSITIVE:
        return await sync_to_async(run_orm)(func, *args)
    return await sync_to_async(run_orm, thread_sensitive=False, executor=orm_executor)(
        func, *args
    )


def get_starlette_client_ip(request) -> str | None:
    return core_utils.resolve_client_ip(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
    )


def authorize_mcp_pat(pat: models.PersonalAccessToken, client_ip: str | None) -> None:
    # Tools only receive the user, so a restriction they can't see must be
    # refused here rather than silently widened. The permission allowlist in
    # pat.scopes is not applied yet: the tool registry adapter filters tools by
    # it (#804), and the placeholder whoami tool needs no permission.
    if pat.allowed_scopes:
        reason = "mcp_entity_bound"
    elif pat.user.is_staff and PermissionEnum.STAFF_ACCESS.value not in pat.scopes:
        reason = "mcp_staff_access_missing"
    elif pat.user.is_support and not (
        {PermissionEnum.STAFF_ACCESS.value, PermissionEnum.SUPPORT_ACCESS.value}
        & set(pat.scopes)
    ):
        reason = "mcp_support_access_missing"
    else:
        return
    # Audited like the shared rejections: the owner otherwise has no way to
    # learn why a token that works on REST is refused here.
    PATAuthentication._emit_authentication_rejected(pat, reason, client_ip)
    raise AuthenticationFailed("Invalid token.")


def verify_personal_access_token(
    raw_token: str, client_ip: str | None
) -> AccessToken | None:
    try:
        result = authenticate_personal_access_token(
            raw_token, client_ip, authorize=authorize_mcp_pat
        )
    except AuthenticationFailed:
        return None
    if result is None:
        return None

    user, pat = result
    return AccessToken(
        token=raw_token,
        client_id=f"pat:{pat.uuid.hex}",
        scopes=list(pat.scopes),
        expires_at=int(pat.expires_at.timestamp()),
        claims={
            "sub": user.uuid.hex,
            "username": user.username,
            "pat_uuid": pat.uuid.hex,
        },
    )


class WaldurTokenVerifier(TokenVerifier):
    """Accepts Personal Access Tokens sent as ``Authorization: Bearer w_...``."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith(models.PAT_PREFIX):
            return None
        client_ip = get_starlette_client_ip(get_http_request())
        return await run_orm_async(verify_personal_access_token, token, client_ip)
