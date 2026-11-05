"""Outbound address checks and the client Waldur uses to call a remote Waldur.

Everything that makes Waldur open a connection to an address it was handed
(a remote Waldur's api_url today) goes through ``check_url`` or
``resolve_and_check``: the URL must be plain http(s), and every address its
host resolves to must pass ``check_address``. ``get_waldur_client`` then pins
the connection to the addresses that passed, so a second DNS answer cannot
send it elsewhere; behind an egress proxy from the environment the request is
checked but not pinned (see ``waldur_core.core.pinned_transport``).
"""

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from django.conf import settings

# The waldur_api_client SDK pulls in a large generated attrs/pydantic model graph
# (~70 MB resident). Import it lazily inside get_waldur_client so it only loads in
# processes that actually build a remote client, not at Django startup. See the
# "Lazy imports for heavy optional backends" section of CLAUDE.md. The pinned
# transport (httpcore) is imported there too, for the same reason.

logger = logging.getLogger(__name__)

INTERNAL_ADDRESS_MESSAGE = "Access to internal IPs is not allowed"
RESOLVE_ERROR_MESSAGE = "Error resolving client hostname"

ALLOWED_SCHEMES = {"http": 80, "https": 443}

# Seconds. The SDK sets no timeout of its own, so a remote that accepts the
# connection and never answers would otherwise hold a worker forever.
REMOTE_CONNECT_TIMEOUT = 10.0
REMOTE_TIMEOUT = 60.0
# Largest file fetch_url reads (offering images and screenshots).
MAX_FETCH_BYTES = 20 * 1024 * 1024

# NAT64 well-known prefix: the last 32 bits are the IPv4 address (RFC 6052).
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# IPv4-translated addresses, ::ffff:0:a.b.c.d (RFC 2765).
_IPV4_TRANSLATED_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")


class ClientValidationError(Exception):
    def __init__(self, message):
        super().__init__(message)
        logger.error(f"Unable to initialize remote client: {message}")


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


def check_address(address):
    try:
        internal = is_internal_address(address)
    except ValueError:
        raise ClientValidationError(RESOLVE_ERROR_MESSAGE)
    if internal:
        raise ClientValidationError(INTERNAL_ADDRESS_MESSAGE)


def resolve_and_check(host, port=None) -> list[str]:
    """Resolve ``host`` and return its addresses once every one has passed.

    One internal address among the answers refuses the whole host, so the
    result does not depend on which address a client would try first.
    """
    try:
        addrinfo = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        raise ClientValidationError(RESOLVE_ERROR_MESSAGE)
    addresses = []
    # Each entry is (family, type, proto, canonname, sockaddr).
    for family, _, _, _, sockaddr in addrinfo:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = sockaddr[0]
        check_address(address)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ClientValidationError(RESOLVE_ERROR_MESSAGE)
    return addresses


def split_url(url, allow_query=False):
    """Check the shape of an outbound URL; return (hostname, port).

    Only plain http(s) URLs are accepted: no credentials, query (unless
    ``allow_query``) or fragment, and nothing that parsers disagree about
    (whitespace, backslashes).
    """
    if not isinstance(url, str):
        raise ClientValidationError(RESOLVE_ERROR_MESSAGE)
    if any(char.isspace() or char == "\\" or ord(char) < 0x20 for char in url):
        raise ClientValidationError("URL contains characters that are not allowed")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        raise ClientValidationError("URL is not valid")
    if not hostname:
        raise ClientValidationError(RESOLVE_ERROR_MESSAGE)
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

    if isinstance(exc, ClientValidationError):
        return str(exc)
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
        transport = transport_for(api_url, {hostname: addresses})
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
    so callers handle both the same way.
    """
    import httpx

    from waldur_core.core.pinned_transport import transport_for

    try:
        hostname, _ = split_url(url, allow_query=True)
        addresses = check_url(url, allow_query=True)
        transport = transport_for(url, {hostname: addresses})
    except (ClientValidationError, httpx.InvalidURL) as exc:
        raise httpx.ConnectError(str(exc)) from None
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(REMOTE_TIMEOUT, connect=REMOTE_CONNECT_TIMEOUT),
        follow_redirects=False,
    ) as client:
        # Ask for the file as is, and refuse it if it comes encoded anyway:
        # httpx would decompress each chunk in full before the size cap below
        # could look at it, so a few hundred bytes on the wire could become
        # hundreds of megabytes in memory.
        with client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}
        ) as response:
            encoding = response.headers.get("content-encoding", "identity")
            if encoding.strip().lower() != "identity":
                raise httpx.DecodingError(
                    f"Refusing a response with Content-Encoding {encoding!r}.",
                    request=response.request,
                )
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_FETCH_BYTES:
                    raise httpx.ReadError(
                        f"Response is larger than {MAX_FETCH_BYTES} bytes."
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
        # Rebuild a read response so callers can use .content and
        # raise_for_status() as with httpx.get(). The headers that described
        # the transfer of the body no longer apply to it.
        headers = httpx.Headers(response.headers)
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            headers.pop(name, None)
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=content,
            request=response.request,
        )
