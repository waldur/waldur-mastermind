"""An httpx transport that connects only to addresses that passed the outbound check.

httpx resolves the host itself when it connects, so checking the host's
addresses beforehand leaves a window: DNS can answer with a public address for
the check and an internal one for the connection. This transport closes it
from below. Its httpcore network backend connects to the addresses
``check_url`` already approved for the host (or, for any other host, resolves
and checks them itself) and hands httpcore a stream to that IP. httpcore still
uses the hostname from the URL for the Host header, TLS SNI and certificate
verification, so nothing else about the request changes.

Egress proxies. When the environment sets a proxy for the target
(``http_proxy`` / ``https_proxy`` / ``all_proxy``, minus ``no_proxy``; the
waldur-helm chart sets these), the backend connects to the proxy and opens a
``CONNECT <checked IP>:<port>`` tunnel, for plain http targets too, so the
proxy never resolves the hostname itself and the pin holds. httpcore then
speaks HTTP, or TLS with the hostname for SNI and verification, through the
tunnel. Which proxy applies is decided with httpx's own rules
(``get_environment_proxies`` and ``URLPattern``, what ``httpx.Client`` uses with
``trust_env=True``). A proxy that refuses CONNECT to IP literals can be given
the hostname instead with ``WALDUR_CORE["OUTBOUND_PROXY_CONNECT_BY_HOSTNAME"]``;
the address check still runs first, but the proxy's own lookup and ACL then
decide where the tunnel goes.

Apart from the two proxy helpers, only public httpx/httpcore API is used:
``httpcore.ConnectionPool`` accepts a ``network_backend``, and
``httpx.BaseTransport`` is the documented way to plug a custom transport into
``httpx.Client``. The request/response mapping below mirrors
``httpx.HTTPTransport``.

Imported lazily by ``waldur_core.core.client``, so httpcore does not load at
Django startup.
"""

import base64
import contextlib
import ssl
import time
import typing

import httpcore
import httpx
from django.conf import settings

# Private to httpx, but they are exactly how httpx.Client(trust_env=True)
# picks a proxy; the tests fail loudly if an httpx upgrade moves them.
from httpx._utils import URLPattern, get_environment_proxies

from waldur_core.core import client as core_client

# Most specific first: the first match wins.
_EXCEPTION_MAP: tuple[tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
)

# At most this many of a host's addresses are tried, within one connect timeout.
MAX_ADDRESSES_TRIED = 4
# A CONNECT response head larger than this is refused.
MAX_PROXY_RESPONSE_HEAD = 16 * 1024


@contextlib.contextmanager
def _httpx_exceptions() -> typing.Iterator[None]:
    try:
        yield
    except Exception as exc:
        for source, target in _EXCEPTION_MAP:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
        raise


def environment_proxy(url: httpx.URL) -> str | None:
    """The proxy the environment sets for ``url``, or None to connect directly."""
    mounts = sorted(
        (
            (URLPattern(pattern), proxy)
            for pattern, proxy in get_environment_proxies().items()
        ),
        key=lambda mount: mount[0],
    )
    for pattern, proxy in mounts:
        if pattern.matches(url):
            return proxy
    return None


class AddressCheckingBackend(httpcore.NetworkBackend):
    """Opens TCP connections only to addresses that pass ``check_address``.

    ``scheme`` is the scheme of the URLs the client calls; with it the backend
    picks the environment's proxy for each host it connects to.
    """

    def __init__(
        self,
        pinned: dict[str, list[str]] | None = None,
        scheme: str = "https",
        connect_by_hostname: bool = False,
    ):
        self._backend = httpcore.SyncBackend()
        self._pinned = {
            host.lower(): list(addresses) for host, addresses in (pinned or {}).items()
        }
        self._scheme = scheme
        self._connect_by_hostname = connect_by_hostname

    def _addresses(self, host: str, port: int) -> list[str]:
        addresses = self._pinned.get(host.lower())
        try:
            if addresses is None:
                return core_client.resolve_and_check(host, port)
            for address in addresses:
                core_client.check_address(address)
            return addresses
        except core_client.ClientValidationError as exc:
            raise httpcore.ConnectError(str(exc)) from None

    def _proxy(self, host: str, port: int) -> httpx.URL | None:
        proxy = environment_proxy(httpx.URL(scheme=self._scheme, host=host, port=port))
        if proxy is None:
            return None
        proxy_url = httpx.URL(proxy)
        if proxy_url.scheme not in ("http", "https"):
            # A SOCKS proxy would need its own tunnel code; refuse rather than
            # bypass the proxy the operator configured.
            raise httpcore.ProxyError(
                f"Only http and https proxies are supported, not {proxy_url.scheme}."
            )
        return proxy_url

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = self._addresses(host, port)[:MAX_ADDRESSES_TRIED]
        proxy = self._proxy(host, port)
        if proxy is not None and self._connect_by_hostname:
            addresses = [host]
        deadline = None if timeout is None else time.monotonic() + timeout
        last_error: Exception | None = None
        for address in addresses:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                if proxy is None:
                    # An IP literal: the socket layer does no further lookup.
                    return self._backend.connect_tcp(
                        address,
                        port,
                        timeout=remaining,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                return self._tunnel(
                    proxy, address, port, remaining, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is None:
            raise httpcore.ConnectTimeout("Connect timeout.")
        raise last_error

    def _tunnel(
        self,
        proxy: httpx.URL,
        address: str,
        port: int,
        timeout: float | None,
        local_address: str | None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None,
    ) -> httpcore.NetworkStream:
        """Connect to ``proxy`` and open a CONNECT tunnel to ``address``:``port``."""
        # The proxy is the operator's own configuration, so its address is
        # not checked.
        proxy_port = proxy.port or (443 if proxy.scheme == "https" else 80)
        stream = self._backend.connect_tcp(
            proxy.host,
            proxy_port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        try:
            if proxy.scheme == "https":
                stream = stream.start_tls(
                    httpx.create_ssl_context(),
                    server_hostname=proxy.host,
                    timeout=timeout,
                )
            authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
            head = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
            if proxy.username or proxy.password:
                credentials = f"{proxy.username}:{proxy.password}".encode()
                head.append(
                    "Proxy-Authorization: Basic "
                    + base64.b64encode(credentials).decode("ascii")
                )
            stream.write(("\r\n".join(head) + "\r\n\r\n").encode(), timeout=timeout)
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = stream.read(4096, timeout=timeout)
                if not chunk:
                    raise httpcore.ProxyError("The proxy closed the connection.")
                response += chunk
                if len(response) > MAX_PROXY_RESPONSE_HEAD:
                    raise httpcore.ProxyError("The proxy answer is too long.")
            head_bytes, _, rest = response.partition(b"\r\n\r\n")
            status_line = head_bytes.split(b"\r\n", 1)[0].decode("latin-1")
            parts = status_line.split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                raise httpcore.ProxyError(f"Unexpected proxy answer: {status_line!r}")
            if not parts[1].startswith("2"):
                raise httpcore.ProxyError(
                    f"The proxy refused the tunnel: {status_line}"
                )
            if rest:
                # Nothing may come through the tunnel before the client speaks.
                raise httpcore.ProxyError("Unexpected data after the proxy answer.")
            return stream
        except BaseException:
            stream.close()
            raise

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed.")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: typing.Iterable[bytes]):
        self._stream = stream

    def __iter__(self) -> typing.Iterator[bytes]:
        with _httpx_exceptions():
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class PinnedTransport(httpx.BaseTransport):
    """httpx transport whose connections go only to checked addresses.

    ``pinned`` maps a hostname to the addresses ``check_url`` approved for it;
    connections to that host use those addresses without resolving it again,
    directly or through a CONNECT tunnel of the environment's proxy.
    """

    def __init__(
        self,
        pinned: dict[str, list[str]] | None = None,
        scheme: str = "https",
        connect_by_hostname: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(),
            network_backend=AddressCheckingBackend(
                pinned, scheme=scheme, connect_by_hostname=connect_by_hostname
            ),
            # httpx's own defaults; httpcore's keep connections forever.
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _httpx_exceptions():
            core_response = self._pool.handle_request(core_request)
        assert isinstance(core_response.stream, typing.Iterable)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_ResponseStream(core_response.stream),
            extensions=core_response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def transport_for(url: str, pinned: dict[str, list[str]]) -> PinnedTransport:
    """The transport for a client of ``url``, whose host ``check_url`` passed.

    ``pinned`` maps that host to the addresses the check approved.
    """
    return PinnedTransport(
        pinned=pinned,
        scheme=httpx.URL(url).scheme,
        connect_by_hostname=bool(
            settings.WALDUR_CORE.get("OUTBOUND_PROXY_CONNECT_BY_HOSTNAME", False)
        ),
    )
