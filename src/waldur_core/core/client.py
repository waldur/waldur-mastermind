"""Outbound address checks and the client Waldur uses to call a remote Waldur.

Everything that makes Waldur open a connection to an address it was handed
(a remote Waldur's api_url today) goes through ``check_url`` or
``resolve_and_check``: the URL must be plain http(s), and every address its
host resolves to must pass ``check_address``. ``get_waldur_client`` then pins
the connection to the addresses that passed, so a second DNS answer cannot
send it elsewhere; behind an egress proxy from the environment the request is
checked but not pinned (see ``waldur_core.core.pinned_transport``).

What one remote Waldur can cost us is bounded too: every response is read
unencoded and up to ``MAX_RESPONSE_BYTES``, and ``remote_call_limits`` bounds a
call made of several requests (an SDK ``sync_all``) in requests, time and
bytes. ``sync_all_pages`` runs ``sync_all`` within ``SYNC_ALL_LIMITS``.

A refused host gets one answer, ``REFUSED_HOST_MESSAGE``, whether it did not
resolve or resolved to an address on the list, so the answer does not tell
users which internal names exist. The reason goes to the log.
"""

import contextlib
import contextvars
import dataclasses
import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

from django.conf import settings

# The waldur_api_client SDK pulls in a large generated attrs/pydantic model graph
# (~70 MB resident). Import it lazily inside get_waldur_client so it only loads in
# processes that actually build a remote client, not at Django startup. See the
# "Lazy imports for heavy optional backends" section of CLAUDE.md. The pinned
# transport (httpcore) is imported there too, for the same reason.

logger = logging.getLogger(__name__)

# The one answer for a host that does not resolve and for one that resolves
# to a refused address.
REFUSED_HOST_MESSAGE = "The URL's host cannot be resolved or is not allowed."

ALLOWED_SCHEMES = {"http": 80, "https": 443}

# Seconds. The SDK sets no timeout of its own, so a remote that accepts the
# connection and never answers would otherwise hold a worker forever.
REMOTE_CONNECT_TIMEOUT = 10.0
REMOTE_TIMEOUT = 60.0
# Largest file fetch_url reads (offering images and screenshots).
MAX_FETCH_BYTES = 20 * 1024 * 1024
# Largest response body read from a remote Waldur, by any client of
# get_waldur_client. A page of 100 offerings with every field, the largest
# answer Waldur reads, is a few hundred kilobytes; offering details with long
# descriptions are well under a megabyte.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Most JSON values one response may hold, as waldur_core.core.pinned_transport
# counts them: commas, colons and brackets, an object counting as 16, plus the
# body's text at one value per 50 bytes of the str Python will store it in
# (four bytes a character once a string holds an emoji). Parsed by the SDK, a
# value costs about 50 bytes (measured: 30-60 for strings, numbers and lists,
# about 800 for an object), so 500,000 is about 25 MB of Python objects,
# whatever the shape of the JSON. Bytes alone do not bound this: an 8 MB array
# of short strings parses to about 90 MB. A page of 100 offerings with every
# field is about 200,000.
MAX_RESPONSE_VALUES = 500_000


@dataclasses.dataclass(frozen=True)
class RemoteCallLimits:
    """Bounds one remote call made of several requests, such as a ``sync_all``.

    ``max_requests`` is a page cap for ``sync_all``, which sends one request
    per page of 100 items; ``timeout`` is in seconds, for the whole call;
    ``max_bytes`` counts every byte the call reads, response heads included,
    and ``max_values`` the JSON values in the bodies (see
    ``MAX_RESPONSE_VALUES``), which is what bounds the memory a parsed answer
    takes: at about 50 bytes a value.
    """

    max_requests: int
    timeout: float
    max_bytes: int
    max_values: int = MAX_RESPONSE_VALUES


# Every sync_all made by sync_all_pages: Celery syncs and the views alike.
# The largest real lists are an offering's users and a remote organization's
# projects, thousands of items, tens of pages; 1000 pages is 100,000 items.
# Two million values is about 100 MB parsed: 20,000 offering users with the
# fields Waldur asks for are about 500,000. Five minutes leaves a task that
# makes a few calls inside the Celery soft time limit of twenty.
SYNC_ALL_LIMITS = RemoteCallLimits(
    max_requests=1000,
    timeout=300,
    max_bytes=50 * 1024 * 1024,
    max_values=2_000_000,
)
# A view answering a user's request with what a remote Waldur says: the
# remote organizations, categories and offerings of the import dialog, a few
# pages at most. Gunicorn kills a worker that is silent for 30 seconds.
REMOTE_VIEW_LIMITS = RemoteCallLimits(
    max_requests=50,
    timeout=25,
    max_bytes=MAX_RESPONSE_BYTES,
    max_values=MAX_RESPONSE_VALUES,
)
# A call made while answering a request, outside the remote views: a push of
# resource options, or a project sync run by a signal handler.
REMOTE_REQUEST_LIMITS = RemoteCallLimits(
    max_requests=20,
    timeout=20,
    max_bytes=MAX_RESPONSE_BYTES,
    max_values=MAX_RESPONSE_VALUES,
)


@dataclasses.dataclass
class RemoteCallBudget:
    """What is left of one ``RemoteCallLimits``; the transport spends it."""

    limits: RemoteCallLimits
    deadline: float
    requests: int = 0
    received: int = 0
    values: int = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


_budgets: contextvars.ContextVar[tuple[RemoteCallBudget, ...]] = contextvars.ContextVar(
    "remote_call_budgets", default=()
)


def active_budgets() -> tuple[RemoteCallBudget, ...]:
    """The budgets of every ``remote_call_limits`` block the caller is in."""
    return _budgets.get()


def spent_budget() -> RemoteCallBudget | None:
    """An active budget with no time, requests or bytes left, if any."""
    now = time.monotonic()
    for budget in _budgets.get():
        if (
            now >= budget.deadline
            or budget.requests >= budget.limits.max_requests
            or budget.received >= budget.limits.max_bytes
        ):
            return budget
    return None


@contextlib.contextmanager
def using_budgets(budgets: tuple[RemoteCallBudget, ...]):
    """Make ``budgets`` the active ones inside the block; for the transport,
    which reads a response after the block that set them has been left."""
    token = _budgets.set(budgets)
    try:
        yield
    finally:
        _budgets.reset(token)


@contextlib.contextmanager
def remote_call_limits(limits: RemoteCallLimits):
    """Bound the requests made inside the block to remote Waldur clients.

    Past a limit the client's transport raises a ``RemoteCallRefused``
    (an ``httpx.TransportError``, see ``waldur_core.core.pinned_transport``)
    and stops. Blocks nest: every enclosing limit applies.
    """
    budget = RemoteCallBudget(limits, deadline=time.monotonic() + limits.timeout)
    token = _budgets.set(_budgets.get() + (budget,))
    try:
        yield budget
    finally:
        _budgets.reset(token)


# What the SDK raises on an answer it cannot parse: invalid JSON, nesting too
# deep, an integer too long to convert, or JSON of the wrong shape.
_PARSE_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RecursionError)


def call_remote(function, *args, limits=None, **kwargs):
    """``function(*args, **kwargs)``, an SDK call, within ``limits``
    (``SYNC_ALL_LIMITS`` by default).

    An answer the SDK cannot parse raises ``InvalidRemoteAnswer``, a
    ``RemoteCallRefused``, so that it is handled as a failed call rather than
    as a server error. An error raised before any request was made is ours,
    and is raised as is.
    """
    from waldur_core.core.remote_errors import InvalidRemoteAnswer

    with remote_call_limits(limits or SYNC_ALL_LIMITS) as budget:
        try:
            return function(*args, **kwargs)
        except _PARSE_ERRORS as exc:
            if not budget.requests:
                raise
            raise InvalidRemoteAnswer(f"{type(exc).__name__}: {exc}"[:500]) from exc


def sync_all_pages(endpoint, *, client, limits=None, **kwargs):
    """``endpoint.sync_all(client=client, **kwargs)`` through ``call_remote``.

    The SDK's ``sync_all`` follows the remote's ``next`` link for as long as
    there is one, and its timeout applies to each read, not to the call.
    """
    return call_remote(endpoint.sync_all, client=client, limits=limits, **kwargs)


# NAT64 well-known prefix: the last 32 bits are the IPv4 address (RFC 6052).
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# IPv4-translated addresses, ::ffff:0:a.b.c.d (RFC 2765).
_IPV4_TRANSLATED_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")


class ClientValidationError(Exception):
    """A URL Waldur will not connect to; the message is safe to show users.

    ``reason`` says why, for the log and tests only: telling users whether a
    host did not resolve or resolved to an internal address would let them
    probe internal names.
    """

    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason or message


def _refused_host(reason, host) -> ClientValidationError:
    # Any signed-in caller can make this happen, so it is not an error of
    # ours: a warning stays out of Sentry, which records ERROR and above.
    logger.warning("Refused outbound connection to %r: %s", host, reason)
    return ClientValidationError(REFUSED_HOST_MESSAGE, reason=reason)


def _embedded_ipv4(ip):
    """The IPv4 address an IPv6 address carries, or None.

    An IPv6 address is never "in" an IPv4 network, so without this
    ``::ffff:127.0.0.1`` would pass every IPv4 range on the list.
    """
    if ip.version != 6:
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_PREFIX or ip in _IPV4_TRANSLATED_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if 1 < int(ip) <= 0xFFFFFFFF:
        # Deprecated IPv4-compatible form, ::a.b.c.d
        return ipaddress.IPv4Address(int(ip))
    return None


def _blocked_networks():
    return [
        ipaddress.ip_network(network, strict=False)
        for network in settings.WALDUR_CORE["SUBNET_BLACKLIST"]
    ]


def is_internal_address(address) -> bool:
    """Whether Waldur must not connect to ``address``.

    ``address`` is an IP address string (a scope id such as ``%eth0`` is
    accepted) or an ``ipaddress`` object. Refused when the address, or the
    IPv4 address it embeds, falls in ``WALDUR_CORE["SUBNET_BLACKLIST"]``.
    The unwrapping only makes the list apply to every encoding of an
    address; what is refused is the operator's choice.
    """
    ip = ipaddress.ip_address(address)
    candidates = [ip]
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        candidates.append(embedded)
    blocked = _blocked_networks()
    return any(candidate in network for candidate in candidates for network in blocked)


def check_address(address, host=None):
    try:
        internal = is_internal_address(address)
    except ValueError:
        raise _refused_host(f"{address!r} is not an IP address", host or address)
    if internal:
        raise _refused_host(f"{address} is an internal address", host or address)


def resolve_and_check(host, port=None) -> list[str]:
    """Resolve ``host`` and return its addresses once every one has passed.

    One internal address among the answers refuses the whole host, so the
    result does not depend on which address a client would try first.
    """
    try:
        addrinfo = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _refused_host(f"it does not resolve ({exc})", host)
    addresses = []
    # Each entry is (family, type, proto, canonname, sockaddr).
    for family, _, _, _, sockaddr in addrinfo:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = sockaddr[0]
        check_address(address, host)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise _refused_host("it has no IP address", host)
    return addresses


def split_url(url, allow_query=False):
    """Check the shape of an outbound URL; return (hostname, port).

    Only plain http(s) URLs are accepted: no credentials, query (unless
    ``allow_query``) or fragment, and nothing that parsers disagree about
    (whitespace, backslashes).
    """
    if not isinstance(url, str):
        raise ClientValidationError("URL is not valid")
    if any(char.isspace() or char == "\\" or ord(char) < 0x20 for char in url):
        raise ClientValidationError("URL contains characters that are not allowed")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        raise ClientValidationError("URL is not valid")
    if not hostname:
        raise ClientValidationError("URL is not valid")
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ClientValidationError("Only http and https URLs are allowed")
    if "@" in parsed.netloc:
        raise ClientValidationError("URL must not contain credentials")
    if "#" in url or ("?" in url and not allow_query):
        raise ClientValidationError("URL must not contain a query or a fragment")
    try:
        port = parsed.port
    except ValueError:
        raise ClientValidationError("URL port is not valid")
    return hostname, port or ALLOWED_SCHEMES[scheme]


def check_url(url, allow_query=False) -> list[str]:
    """Refuse ``url`` unless it is a plain http(s) URL whose host resolves to
    public addresses only; return those addresses."""
    hostname, port = split_url(url, allow_query=allow_query)
    return resolve_and_check(hostname, port)


def get_api_root(api_url):
    """``https://host/api/`` -> ``https://host``; other URLs lose only trailing slashes."""
    return api_url.rstrip("/").removesuffix("/api")


def describe_remote_error(exc) -> str:
    """A message about a failed remote Waldur call that is safe to show users.

    The SDK's errors carry the remote response body and URL; those go to the
    log, never into an API response or a field users can read.
    """
    from waldur_api_client.errors import UnexpectedStatus

    from waldur_core.core.remote_errors import RemoteCallRefused

    if isinstance(exc, ClientValidationError):
        return str(exc)
    if isinstance(exc, RemoteCallRefused):
        logger.warning("Remote Waldur call stopped: %s", exc.reason)
        return exc.user_message
    if isinstance(exc, UnexpectedStatus):
        message = f"The remote Waldur answered with status {exc.status_code}."
    else:
        message = "The remote Waldur could not be reached."
    logger.warning("Remote Waldur call failed: %s", exc)
    return message


def get_waldur_client(api_url, token):
    import httpx
    from waldur_api_client.client import AuthenticatedClient

    from waldur_core.core.pinned_transport import transport_for

    hostname, _ = split_url(api_url)
    addresses = check_url(api_url)
    try:
        transport = transport_for(
            api_url,
            {hostname: addresses},
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_response_values=MAX_RESPONSE_VALUES,
        )
    except httpx.InvalidURL:
        raise ClientValidationError("URL is not valid")
    return AuthenticatedClient(
        base_url=get_api_root(api_url),
        token=token,
        timeout=httpx.Timeout(REMOTE_TIMEOUT, connect=REMOTE_CONNECT_TIMEOUT),
        # A redirect could point anywhere; the remote API never needs one.
        follow_redirects=False,
        # With a transport passed, httpx ignores the client's verify_ssl: TLS
        # verification is set by the transport's SSL context, which verifies.
        httpx_args={"transport": transport},
    )


def fetch_url(url):
    """GET a URL a remote Waldur handed us, such as an offering image.

    The URL gets the same address check and pinning as the remote client.
    A refused URL raises ``httpx.ConnectError``, as an unreachable one does,
    so callers handle both the same way. Inside a spent ``remote_call_limits``
    block it raises ``RemoteCallRefused`` before resolving the host: no budget
    can interrupt a DNS lookup once it has started.
    """
    import httpx

    from waldur_core.core.pinned_transport import refuse_if_spent, transport_for

    refuse_if_spent()
    try:
        hostname, _ = split_url(url, allow_query=True)
        addresses = check_url(url, allow_query=True)
        transport = transport_for(
            url, {hostname: addresses}, max_response_bytes=MAX_FETCH_BYTES
        )
    except (ClientValidationError, httpx.InvalidURL) as exc:
        raise httpx.ConnectError(str(exc)) from None
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(REMOTE_TIMEOUT, connect=REMOTE_CONNECT_TIMEOUT),
        follow_redirects=False,
    ) as client:
        # The transport asks for the file unencoded, refuses it encoded and
        # stops reading past MAX_FETCH_BYTES; the body is read in full here.
        return client.get(url)
