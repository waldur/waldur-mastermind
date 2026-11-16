import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from oauth2_provider.models import get_access_token_model
from rest_framework.exceptions import AuthenticationFailed
from starlette.authentication import AuthCredentials
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from waldur_core.core import models
from waldur_core.core import utils as core_utils
from waldur_core.core.authentication import (
    PATAuthentication,
    authenticate_personal_access_token,
)
from waldur_core.permissions.enums import PermissionEnum
from waldur_mastermind.mcp import oauth
from waldur_mastermind.mcp.models import ConnectedApp

# Shown to the user only, so up to ten minutes stale is fine, as for PATs.
USAGE_DEBOUNCE = timedelta(minutes=10)


def drop_broken_connections():
    # Django drops broken connections only around its own requests, so this
    # long-lived process does it itself; otherwise a database restart would fail
    # every later call. close_old_connections() would also close healthy ones,
    # since CONN_MAX_AGE is 0, and reconnect on every call.
    for connection in connections.all(initialized_only=True):
        if connection.connection is not None and not connection.is_usable():
            connection.close()


# Without it, a slow query holds its pool thread for as long as it runs, and
# the gate and token checks queue behind it. 0 turns it off.
DEFAULT_STATEMENT_TIMEOUT = 30
# Postgres stores the setting in milliseconds as a 32-bit integer.
MAX_STATEMENT_TIMEOUT = (2**31 - 1) // 1000
statement_timeout_ms = DEFAULT_STATEMENT_TIMEOUT * 1000


def configure_statement_timeout(seconds: int) -> None:
    global statement_timeout_ms
    statement_timeout_ms = seconds * 1000


@contextmanager
def bounded_statements():
    """Run the block in a transaction whose statements time out.

    Set for the transaction, not the session: behind a pooler in transaction
    mode a session setting would reach other clients' queries, and a startup
    option is refused outright.
    """
    database = transaction.get_connection()
    if not statement_timeout_ms or database.vendor != "postgresql":
        yield
        return
    with transaction.atomic():
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                [str(statement_timeout_ms)],
            )
        yield


def run_orm(func, *args):
    """Call an ORM function on a live connection, with bounded statements."""
    drop_broken_connections()
    with bounded_statements():
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


# No default: a verifier running without ClientIPMiddleware must fail, not treat
# every caller as having no address.
client_ip_var: ContextVar[str | None] = ContextVar("mcp_client_ip")


class ClientIPMiddleware:
    """Hands the caller's address to the verifier, which only receives the token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        reset_token = client_ip_var.set(get_starlette_client_ip(HTTPConnection(scope)))
        try:
            await self.app(scope, receive, send)
        finally:
            client_ip_var.reset(reset_token)


def authorize_mcp_pat(pat: models.PersonalAccessToken, client_ip: str | None) -> None:
    # Tools only receive the user, so a restriction they can't see must be
    # refused here rather than silently widened. The permission allowlist in
    # pat.scopes is applied per tool, in tools.get_visible_definitions.
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


def load_oauth_token(raw_token: str):
    """The DOT access token for ``raw_token``, if it is valid on /mcp."""
    checksum = hashlib.sha256(raw_token.encode()).hexdigest()
    token = (
        get_access_token_model()
        .objects.select_related("user", "application")
        .filter(token_checksum=checksum)
        .first()
    )
    if (
        token is None
        # DCR registration tokens have neither a user nor a resource.
        or token.user is None
        or not token.user.is_active
        or token.application is None
        or token.is_expired()
        or oauth.READ_SCOPE not in token.scope.split()
    ):
        return None
    # Read only for a known token: settings aren't cached, and anyone can send
    # an unknown one.
    allowed = oauth.get_allowed_resources()
    # DOT treats a token without a resource as valid anywhere.
    if not any(oauth.is_allowed_resource(uri, allowed) for uri in token.resource):
        return None
    return token


def record_oauth_use(token) -> None:
    now = timezone.now()
    # Only updates the existing row, so a revoked connection stays deleted.
    ConnectedApp.objects.filter(
        Q(last_used_at__isnull=True) | Q(last_used_at__lt=now - USAGE_DEBOUNCE),
        user_id=token.user_id,
        application_id=token.application_id,
    ).update(last_used_at=now)


def verify_oauth_token(raw_token: str) -> AccessToken | None:
    token = load_oauth_token(raw_token)
    if token is None:
        return None
    scopes = token.scope.split()
    missing_scope = oauth.missing_elevated_scope(token.user, scopes)
    if not missing_scope:
        record_oauth_use(token)
    return AccessToken(
        token=raw_token,
        client_id=f"oauth:{token.application.client_id}",
        scopes=scopes,
        expires_at=int(token.expires.timestamp()),
        subject=token.user.uuid.hex,
        claims={
            "username": token.user.username,
            "oauth_token_id": token.pk,
            # Refused by AuthMiddleware with insufficient_scope, not as invalid.
            "missing_scope": missing_scope,
        },
    )


def verify_token(raw_token: str, client_ip: str | None) -> AccessToken | None:
    """A Personal Access Token (``w_...``) or a DOT access token."""
    if raw_token.startswith(models.PAT_PREFIX):
        return verify_personal_access_token(raw_token, client_ip)
    return verify_oauth_token(raw_token)


def authenticate(
    raw_token: str | None, client_ip: str | None, host: str | None
) -> tuple[AccessToken | None, str | None]:
    """The token, and the resource URL a challenge names if it is refused.

    Both in one trip to the few ORM threads, which every caller shares.
    """
    token = verify_token(raw_token, client_ip) if raw_token else None
    if token is not None and not token.claims.get("missing_scope"):
        return token, None
    return token, oauth.get_resource_url(host)


def get_bearer_token(connection: HTTPConnection) -> str | None:
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


class AuthMiddleware:
    """Authenticates /mcp with the challenges MCP clients follow to sign in.

    In place of the SDK's: it takes the resource URL and scopes once, and leaves
    the scope out of insufficient_scope.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        connection = HTTPConnection(scope)
        raw_token = get_bearer_token(connection)
        token, resource = await run_orm_async(
            authenticate,
            raw_token,
            client_ip_var.get(),
            connection.headers.get("host"),
        )
        if token is None:
            # RFC 6750: no error code when the client sent no credentials.
            response = challenge(
                resource,
                401,
                "invalid_token" if raw_token else None,
                "Authentication required.",
            )
        elif missing_scope := token.claims.get("missing_scope"):
            response = challenge(
                resource,
                403,
                "insufficient_scope",
                f"This account needs the {missing_scope} scope.",
                required_scope=f"{oauth.READ_SCOPE} {missing_scope}",
            )
        else:
            scope["user"] = AuthenticatedUser(token)
            scope["auth"] = AuthCredentials(token.scopes)
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


def challenge(resource, status_code, error, description, required_scope=None):
    params = {"error": error, "error_description": description} if error else {}
    if required_scope:
        params["scope"] = required_scope
    if resource:
        params["resource_metadata"] = oauth.get_resource_metadata_url(resource)
    header = ", ".join(f'{key}="{value}"' for key, value in params.items())
    return JSONResponse(
        {"error": error or "invalid_token", "error_description": description},
        status_code=status_code,
        headers={"WWW-Authenticate": f"Bearer {header}".rstrip()},
    )
