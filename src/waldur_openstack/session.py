import base64
import hashlib
import logging
import os.path
import tempfile
import time
from urllib.parse import urlsplit

from cinderclient import exceptions as cinder_exceptions
from cinderclient.v3 import client as cinder_client
from constance import config
from django.core.cache import cache
from glanceclient import exc as glance_exceptions
from glanceclient.v2 import client as glance_client
from keystoneauth1 import exceptions as keystoneauth_exceptions
from keystoneauth1 import session as keystone_session
from keystoneauth1.identity import v3
from keystoneclient import exceptions as keystone_exceptions
from keystoneclient.v3 import client as keystone_client
from neutronclient.client import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from novaclient import client as nova_client
from novaclient import exceptions as nova_exceptions
from novaclient.v2 import client as nova2_client

from waldur_core.core.utils import QuietSession
from waldur_core.structure.backend import current_backend_action
from waldur_openstack.exceptions import (
    OpenStackAuthorizationFailed,
    OpenStackBackendError,
)

logger = logging.getLogger(__name__)
# Dedicated logger for per-call OpenStack HTTP traces; operators can raise it
# to INFO to see one line per outgoing call without enabling DEBUG on every
# keystoneauth/requests module.
call_logger = logging.getLogger("waldur_openstack.calls")


SESSION_LIFETIME = 10 * 60 * 60


class TimedSession(keystone_session.Session):
    """keystoneauth Session that emits one log line per HTTP call.

    Each line carries method, host+path, status code and elapsed milliseconds,
    plus the currently-active backend action (set by `@log_backend_action`) so
    a slow tenant teardown can be unpicked into individual OpenStack calls.

    The class is a drop-in for `keystone_session.Session`; all keystoneauth
    SDK clients route their HTTP through `Session.request`, so subclassing
    once at session construction time instruments every downstream SDK.
    """

    def request(self, url, method, **kwargs):
        start = time.perf_counter()
        status: int | str = "-"
        error: str | None = None
        response = None
        try:
            response = super().request(url, method, **kwargs)
            status = response.status_code
            return response
        except Exception as exc:
            error = type(exc).__name__
            # keystoneauth raises typed exceptions (e.g. NotFound) that carry
            # the HTTP status — surface it so 4xx/5xx still shows up cleanly.
            status = getattr(exc, "http_status", None) or status
            response = getattr(exc, "response", None)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            # Errors always log so failures aren't hidden by the threshold.
            # SDK clients (nova/cinder/…) pass raise_exc=False to keystoneauth
            # and handle HTTP failures themselves, so 4xx/5xx return as a
            # status code rather than as a raised exception — treat both as
            # "always log".
            failed = bool(error) or (isinstance(status, int) and status >= 400)
            should_log = failed
            if not should_log:
                try:
                    if config.OPENSTACK_LOG_CALLS_ENABLED:
                        threshold_ms = config.OPENSTACK_LOG_CALLS_THRESHOLD_MS or 0
                        should_log = elapsed_ms >= threshold_ms
                except Exception:
                    # Constance failures (DB unavailable during boot, etc.)
                    # must not break OpenStack calls. Stay quiet on success path.
                    should_log = False
            if should_log:
                # Defer URL parsing and auth/context lookups to the log path
                # so calls that won't be emitted (the default, disabled case)
                # don't pay for them.
                # SDK clients pass relative paths; the resolved absolute URL
                # is only available on the response. Prefer it; fall back to
                # the caller's url. Strip the query string to avoid leaking
                # tokens or other sensitive params into logs.
                resolved = getattr(response, "url", None) or url
                try:
                    parts = urlsplit(resolved)
                    if parts.netloc:
                        short_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
                    else:
                        short_url = parts.path or resolved
                except Exception:
                    short_url = resolved
                action = current_backend_action.get()
                project_id = getattr(
                    getattr(self.auth, "auth_ref", None), "project_id", None
                ) or getattr(self.auth, "project_id", None)
                call_logger.info(
                    "OpenStack %s %s -> %s in %.0fms (action=%s, project=%s%s)",
                    method,
                    short_url,
                    status,
                    elapsed_ms,
                    action or "-",
                    project_id or "-",
                    f", error={error}" if error else "",
                    extra={
                        "openstack_method": method,
                        "openstack_url": short_url,
                        "openstack_status": status,
                        "openstack_duration_ms": round(elapsed_ms, 1),
                        "openstack_project_id": project_id,
                        "openstack_action": action,
                        "openstack_error": error,
                    },
                )


def get_cached_session_key(credentials: dict[str, str]):
    # copy from https://github.com/openstack/keystoneauth/blob/master/keystoneauth1/identity/base.py#L642
    hasher = hashlib.sha256()

    for k, v in sorted(credentials.items()):
        if v is not None:
            if isinstance(k, str):
                k = k.encode("utf-8")
            if isinstance(v, str):
                v = v.encode("utf-8")

            hasher.update(k)
            hasher.update(v)

    key = base64.b64encode(hasher.digest()).decode("utf-8")

    return "OPENSTACK_SESSION_%s" % key


def get_certificate_filename(data):
    if not isinstance(data, bytes):
        data = data.encode("utf-8")
    cert_hash = hashlib.sha256(data).hexdigest()
    return os.path.join(tempfile.gettempdir(), f"waldur-certificate-{cert_hash}.pem")


def get_credentials(settings, tenant=None):
    domain_name = settings.domain or "Default"
    credentials = {
        "auth_url": settings.backend_url,
        "username": settings.username,
        "password": settings.password,
        "user_domain_name": domain_name,
    }
    if tenant:
        if not tenant.backend_id:
            raise OpenStackBackendError("Tenant has no backend_id")
        credentials["project_id"] = tenant.backend_id
        credentials["username"] = tenant.user_username
        credentials["password"] = tenant.user_password
    else:
        credentials["project_domain_name"] = domain_name
        credentials["project_name"] = settings.get_option("tenant_name")

    auth_type = settings.get_option("auth_type") or "password"
    credentials["auth_type"] = auth_type
    return credentials


def recover_cached_session(cached_session: dict[str, str], verify_ssl=False):
    auth_state = cached_session.pop("auth_state")
    auth_method = v3.Token(**cached_session)
    auth_method.set_auth_state(auth_state)
    return TimedSession(auth=auth_method, verify=verify_ssl)


def create_session(credentials: dict[str, str], verify_ssl=False):
    http_session = None
    if not verify_ssl:
        http_session = QuietSession()
        http_session.verify = False

    credentials = dict(credentials)
    auth_type = credentials.pop("auth_type", "password")

    if auth_type == "v3applicationcredential":
        auth = v3.ApplicationCredential(
            auth_url=credentials["auth_url"],
            application_credential_id=credentials["username"],
            application_credential_secret=credentials["password"],
        )
    elif auth_type == "password":
        auth = v3.Password(**credentials)
    else:
        raise OpenStackBackendError(f"Unsupported auth_type: {auth_type}")

    ks_session = TimedSession(
        auth=auth,
        verify=verify_ssl,
        session=http_session,
    )

    try:
        # This will eagerly sign in throwing AuthorizationFailure on bad credentials
        ks_session.get_auth_headers()
    except keystone_exceptions.ClientException as e:
        raise OpenStackAuthorizationFailed(e)

    return ks_session


def get_verify_ssl(settings):
    verify_ssl = settings.get_option("verify_ssl")
    client_cert = settings.get_option("certificate")
    if client_cert:
        file_path = get_certificate_filename(client_cert)
        if not os.path.isfile(file_path):
            with open(file_path, "w") as fh:
                fh.write(client_cert)
        verify_ssl = file_path
    return verify_ssl


def get_cached_session(session: keystone_session.Session):
    cached_session = {}
    for key in (
        "auth_url",
        "project_id",
        "project_name",
        "project_domain_name",
    ):
        cached_session[key] = getattr(session.auth, key)
    cached_session["token"] = session.auth.auth_ref.auth_token
    cached_session["auth_state"] = session.auth.get_auth_state()
    return cached_session


def get_keystone_session(settings, tenant=None):
    credentials = get_credentials(settings, tenant)
    verify_ssl = get_verify_ssl(settings)

    session_key = get_cached_session_key(credentials)
    cached_session = cache.get(session_key)
    # try to get session from cache
    try:
        logger.debug("Trying to recover OpenStack session.")
        recovered_session = recover_cached_session(cached_session, verify_ssl)
        if recovered_session.auth.auth_ref.will_expire_soon(60 * 10):
            cache.delete(session_key)
        else:
            return recovered_session
    except (AttributeError, IndexError, keystoneauth_exceptions.ClientException):
        # A cache miss / expired token is normal operation: we simply
        # re-authenticate below. Log at debug to avoid flooding warnings on
        # every session acquisition (one per tenant pull).
        logger.debug("Unable to recover OpenStack session, deleting cache.")
        cache.delete(session_key)
        pass

    # create new token if session is not cached or expired
    ks_session = create_session(credentials, verify_ssl)
    cached_session = get_cached_session(ks_session)

    cache.set(session_key, cached_session, SESSION_LIFETIME)
    return ks_session


def get_keystone_client(session):
    return keystone_client.Client(
        session=session, endpoint_filter={"interface": "public"}
    )


class PlacementClient:
    """Thin wrapper around a keystoneauth session for the Placement REST API.

    Why not openstacksdk's placement proxy: we only need a handful of read
    endpoints, keystoneauth's session.get already does endpoint discovery +
    auth + JSON parsing, and a hand-rolled wrapper is trivial to mock in tests.
    """

    SERVICE_TYPE = "placement"
    # Pinned microversion. 1.36 covers everything Waldur reads:
    #   - parent_provider_uuid / root_provider_uuid on resource_providers (1.14)
    #   - /resource_providers/{uuid}/traits (1.6)
    #   - /resource_providers/{uuid}/aggregates (1.1)
    #   - /allocation_candidates with required= filter (1.17, full at 1.31)
    #   - /allocations/{consumer_uuid} (1.0)
    # The default (1.0) silently drops several of these fields, so the header
    # is load-bearing — without it, parent_provider_uuid is missing from
    # resource_providers responses and traits/aggregates 404.
    MICROVERSION = "1.36"

    def __init__(self, session: keystone_session.Session):
        self.session = session

    def _get(self, path, **params):
        # Some clouds register Placement only on the internal interface, so
        # try public first and fall back. Re-raise as OpenStackBackendError on
        # full failure so callers can decide whether to abort or skip.
        headers = {
            "OpenStack-API-Version": f"placement {self.MICROVERSION}",
            "User-Agent": "waldur",
        }
        last_error = None
        for interface in ("public", "internal"):
            try:
                response = self.session.get(
                    path,
                    endpoint_filter={
                        "service_type": self.SERVICE_TYPE,
                        "interface": interface,
                    },
                    params=params or None,
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()
            except keystoneauth_exceptions.EndpointNotFound as e:
                last_error = e
                continue
            except keystoneauth_exceptions.ClientException as e:
                raise OpenStackBackendError(e)
        raise OpenStackBackendError(
            f"Placement service is not registered in the catalog (public/internal): {last_error}"
        )

    def list_resource_providers(self, member_of=None):
        params = {}
        if member_of:
            params["member_of"] = "in:" + ",".join(member_of)
        return self._get("/resource_providers", **params).get("resource_providers", [])

    def get_inventories(self, rp_uuid):
        return self._get(f"/resource_providers/{rp_uuid}/inventories").get(
            "inventories", {}
        )

    def get_usages(self, rp_uuid):
        return self._get(f"/resource_providers/{rp_uuid}/usages").get("usages", {})

    def get_traits(self, rp_uuid):
        """Return the list of trait names associated with a resource provider.

        Placement responds with ``{"traits": [...], "resource_provider_generation": N}``;
        we only surface the names. Requires microversion 1.6+ (covered by the
        pinned 1.36).
        """
        return self._get(f"/resource_providers/{rp_uuid}/traits").get("traits", [])

    def list_allocation_candidates(self, resources, required=None, limit=None):
        """Pre-flight scheduler check: ask Placement which resource providers
        could currently satisfy a request for the given resources (and traits,
        if `required` is provided). Used by WAL-9893 to fail orders early on
        a fully-booked cloud instead of letting Nova fail provisioning later.

        Args:
            resources: dict like ``{"VCPU": 4, "MEMORY_MB": 8192}``.
            required: optional iterable of trait names. Sent as comma-joined
                ``required=`` query string. Trait filtering needs Placement
                microversion 1.17+ (covered by the pinned 1.36).
            limit: optional cap on returned candidates.

        Returns the raw Placement response dict with `allocation_requests` and
        `provider_summaries` keys; callers project as needed.
        """
        params = {
            "resources": ",".join(f"{k}:{v}" for k, v in resources.items()),
        }
        if required:
            params["required"] = ",".join(required)
        if limit is not None:
            params["limit"] = str(limit)
        return self._get("/allocation_candidates", **params)

    def get_allocations(self, consumer_uuid):
        """Return Placement's allocation record for a consumer (e.g. a Nova
        server UUID). Returns the raw `allocations` dict shape from Placement::

            {
              "<rp_uuid>": {
                "resources": {"VCPU": 1, "MEMORY_MB": 1024, "DISK_GB": 10},
                "generation": 23,
              },
              ...
            }

        Empty dict for unknown consumers — Placement returns 200 with empty
        `allocations` rather than 404.
        """
        return self._get(f"/allocations/{consumer_uuid}").get("allocations", {})


def get_placement_client(session: keystone_session.Session) -> PlacementClient:
    return PlacementClient(session)


def get_nova_client(session: keystone_session.Session) -> "nova2_client.Client":
    # 2.87 is required for volume-backed instance rescue; pinned exactly because
    # 2.88 removes capacity fields from /os-hypervisors/* (consumed by
    # pull_hypervisors and pull_service_settings_quotas).
    try:
        return nova_client.Client(
            version="2.87",
            session=session,
            endpoint_type="publicURL",
        )
    except nova_exceptions.ClientException as e:
        logger.exception("Failed to create nova client: %s", e)
        raise OpenStackBackendError(e)


def get_neutron_client(session: keystone_session.Session):
    try:
        return neutron_client.Client(session=session)
    except neutron_exceptions.NeutronClientException as e:
        logger.exception("Failed to create neutron client: %s", e)
        raise OpenStackBackendError(e)


def get_cinder_client(session: keystone_session.Session, api_version=None):
    try:
        return cinder_client.Client(session=session, api_version=api_version)
    except cinder_exceptions.ClientException as e:
        logger.exception("Failed to create cinder client: %s", e)
        raise OpenStackBackendError(e)


def get_glance_client(session: keystone_session.Session):
    try:
        return glance_client.Client(session=session)
    except glance_exceptions.ClientException as e:
        logger.exception("Failed to create glance client: %s", e)
        raise OpenStackBackendError(e)
