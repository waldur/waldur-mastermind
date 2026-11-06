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

Bounds. Requests go out with ``Accept-Encoding: identity`` and a response
that comes encoded anyway is refused before its body is read: httpx would
decode each chunk in full before anything could count it, and chained
encodings expand a few hundred bytes into gigabytes. A body is read up to
``max_response_bytes``, and the budgets of the ``remote_call_limits`` blocks
the request is made in are spent here: one request each, the bytes read, and
their deadline, which also shortens the request's own timeouts. Each refusal
is a ``RemoteCallRefused``, an ``httpx.TransportError`` as a failed connection
is, so callers that handle one handle the other.

Imported lazily by ``waldur_core.core.client``, so httpcore does not load at
Django startup.
"""

import base64
import contextlib
import re
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
from waldur_core.core.remote_errors import (
    EncodedResponseRefused,
    InvalidRemoteAnswer,
    RemoteCallRefused,
    RemoteCallTimeout,
    ResponseTooLarge,
    TooManyRequests,
)

__all__ = [
    "EncodedResponseRefused",
    "InvalidRemoteAnswer",
    "PinnedTransport",
    "RemoteCallRefused",
    "RemoteCallTimeout",
    "ResponseTooLarge",
    "TooManyRequests",
    "refuse_if_spent",
    "transport_for",
]

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


Budgets = tuple[core_client.RemoteCallBudget, ...]


def _past_deadline(budget: core_client.RemoteCallBudget) -> RemoteCallTimeout:
    return RemoteCallTimeout(f"past the {budget.limits.timeout}s deadline of the call")


def refuse_if_spent() -> None:
    """Refuse to start another request once an active budget is spent.

    For work done before a request reaches the transport, such as resolving
    a URL's host, which no budget can interrupt.
    """
    budget = core_client.spent_budget()
    if budget is None:
        return
    if time.monotonic() >= budget.deadline:
        raise _past_deadline(budget)
    if budget.requests >= budget.limits.max_requests:
        raise TooManyRequests(
            f"{budget.limits.max_requests} requests of the call already made"
        )
    raise ResponseTooLarge(f"{budget.limits.max_bytes} bytes of the call already read")


def _spend_request(budgets: Budgets) -> None:
    now = time.monotonic()
    for budget in budgets:
        if now >= budget.deadline:
            raise _past_deadline(budget)
        budget.requests += 1
        if budget.requests > budget.limits.max_requests:
            raise TooManyRequests(
                f"more than {budget.limits.max_requests} requests in one call"
            )


def _spend_bytes(budgets: Budgets, size: int) -> None:
    for budget in budgets:
        budget.received += size
        if budget.received > budget.limits.max_bytes:
            raise ResponseTooLarge(
                f"more than {budget.limits.max_bytes} bytes in one call"
            )


def _spend_values(budgets: Budgets, values: int) -> None:
    now = time.monotonic()
    for budget in budgets:
        budget.values += values
        if budget.values > budget.limits.max_values:
            raise ResponseTooLarge(
                f"more than {budget.limits.max_values} JSON values in one call"
            )
        if now >= budget.deadline:
            raise _past_deadline(budget)


def _timeouts_within(timeouts: dict | None, budgets: Budgets) -> dict | None:
    """``timeouts`` (httpx's per-request timeout extension), each cut to the
    time left before the nearest deadline."""
    if not budgets:
        return timeouts
    # Not zero: a zero socket timeout means non-blocking, not "no time left".
    remaining = max(min(budget.remaining() for budget in budgets), 0.01)
    return {
        name: remaining if value is None else min(value, remaining)
        for name, value in (
            timeouts or dict.fromkeys(("connect", "read", "write", "pool"))
        ).items()
    }


@contextlib.contextmanager
def _deadline_errors(budgets: Budgets) -> typing.Iterator[None]:
    """A timeout cut short by a call's deadline is that call's timeout."""
    try:
        yield
    except httpx.TimeoutException as exc:
        if any(time.monotonic() >= budget.deadline for budget in budgets):
            raise RemoteCallTimeout(f"deadline reached: {exc}") from exc
        raise


def _refuse_encoded(headers: httpx.Headers) -> None:
    for encoding in headers.get_list("content-encoding", split_commas=True):
        if encoding.strip().lower() not in ("", "identity"):
            raise EncodedResponseRefused(f"Content-Encoding {encoding!r}")


def _refuse_declared_size(
    headers: httpx.Headers, max_bytes: int | None, budgets: Budgets
) -> None:
    length = headers.get("content-length", "")
    if not length.isdigit():
        return
    limits = [budget.limits.max_bytes - budget.received for budget in budgets]
    if max_bytes is not None:
        limits.append(max_bytes)
    if limits and int(length) > min(limits):
        raise ResponseTooLarge(f"Content-Length {length} over {min(limits)}")


def _json_values(chunk: bytes) -> int:
    """What the JSON in ``chunk`` costs once the SDK has parsed it, in values.

    Every value after the first in an array or object follows a comma, every
    object value follows a colon, and every container opens with a bracket,
    so these bytes bound the count; inside strings they are counted too,
    which only overestimates. A scalar or a list parses to 30-60 bytes, an
    object to an SDK model of about 800, so an object counts as
    ``OBJECT_WEIGHT`` values and a value stands for about 50 bytes.
    """
    return (
        chunk.count(b",")
        + chunk.count(b":")
        + chunk.count(b"[")
        + OBJECT_WEIGHT * chunk.count(b"{")
    )


def _char_width(chunk: bytes) -> int:
    """Bytes per character of a Python str holding text like ``chunk``.

    A str stores every character at the width of its widest one: one byte for
    ASCII, two for the Basic Multilingual Plane, four beyond it. One emoji in
    a long string makes every character of it four bytes, so a body's text is
    charged at the widest width seen in it so far. Two bytes is assumed for
    any non-ASCII text below the astral planes, though Latin-1 needs one.
    """
    if chunk.isascii():
        return 1
    return 4 if _ASTRAL_LEAD.search(chunk) else 2


class _BudgetedStream(httpcore.NetworkStream):
    """A connection whose every read and write is held to the active budgets.

    Each read is cut to the time left before the nearest deadline, and every
    byte read, response heads included, counts against the budgets' bytes.
    httpcore reads a head in many small reads, each with the full read
    timeout, and reads any number of 1xx heads before the response, so
    without this a server could hold a request long past its deadline or
    send unbounded heads. The budgets are looked up at each call, as a pooled
    connection serves one call after another.
    """

    def __init__(self, stream: httpcore.NetworkStream):
        self._stream = stream

    @staticmethod
    def _timeout(timeout: float | None, error: type[Exception]) -> float | None:
        budgets = core_client.active_budgets()
        if not budgets:
            return timeout
        remaining = min(budget.remaining() for budget in budgets)
        if remaining <= 0:
            raise error("The deadline of the remote call has passed.")
        return remaining if timeout is None else min(timeout, remaining)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data = self._stream.read(
            max_bytes, timeout=self._timeout(timeout, httpcore.ReadTimeout)
        )
        _spend_bytes(core_client.active_budgets(), len(data))
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(
            buffer, timeout=self._timeout(timeout, httpcore.WriteTimeout)
        )

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        return _BudgetedStream(
            self._stream.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                timeout=self._timeout(timeout, httpcore.ConnectTimeout),
            )
        )

    def get_extra_info(self, info: str) -> typing.Any:
        return self._stream.get_extra_info(info)


# At most this many of a host's addresses are tried, within one connect timeout.
MAX_ADDRESSES_TRIED = 4
# A CONNECT response head larger than this is refused.
MAX_PROXY_RESPONSE_HEAD = 16 * 1024
# A parsed object costs about this many scalars (see _json_values).
OBJECT_WEIGHT = 16
# What one value stands for, in bytes of parsed Python objects; the text of a
# body is charged at this rate too (see _char_width).
BYTES_PER_VALUE = 50
# UTF-8 lead bytes of characters beyond the Basic Multilingual Plane.
_ASTRAL_LEAD = re.compile(rb"[\xf0-\xf7]")
# What a request made outside any budget may read on top of its body: the
# response head and any 1xx heads before it.
MAX_HEAD_BYTES = 1024 * 1024


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
        return _BudgetedStream(
            self._connect_tcp(host, port, timeout, local_address, socket_options)
        )

    def _connect_tcp(
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
    """A response body, read within the budgets of the request it answers.

    ``wire_counted`` says the connection already counts every byte against
    the budgets (``_BudgetedStream``); a body that did not come through one,
    as from a test double, is counted here instead.
    """

    def __init__(
        self,
        stream: typing.Iterable[bytes],
        max_bytes: int | None = None,
        budgets: Budgets = (),
        max_values: int | None = None,
        wire_counted: bool = False,
    ):
        self._stream = stream
        self._max_bytes = max_bytes
        self._budgets = budgets
        self._max_values = max_values
        self._wire_counted = wire_counted

    def __iter__(self) -> typing.Iterator[bytes]:
        size = values = text_charged = 0
        width = 1
        chunks = iter(self._stream)
        with _deadline_errors(self._budgets), _httpx_exceptions():
            while True:
                # httpx reads the body after handle_request has returned,
                # outside the block that set the budgets.
                with core_client.using_budgets(self._budgets):
                    chunk = next(chunks, None)
                if chunk is None:
                    return
                size += len(chunk)
                if self._max_bytes is not None and size > self._max_bytes:
                    raise ResponseTooLarge(f"body over {self._max_bytes} bytes")
                if not self._wire_counted:
                    _spend_bytes(self._budgets, len(chunk))
                # Only JSON is counted: in an image these bytes mean nothing.
                counted = 0
                if self._max_values is not None:
                    width = max(width, _char_width(chunk))
                    # The text itself, at the width Python will store it:
                    # charged again for the earlier chunks when it widens.
                    text_values = size * width // BYTES_PER_VALUE
                    counted = _json_values(chunk) + text_values - text_charged
                    text_charged = text_values
                    values += counted
                    if values > self._max_values:
                        raise ResponseTooLarge(
                            f"body over {self._max_values} JSON values"
                        )
                _spend_values(self._budgets, counted)
                yield chunk

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class _RemoteResponse(httpx.Response):
    """A response whose JSON, if unreadable, is a refused remote call.

    Invalid JSON, nesting too deep for the parser and integers too long to
    convert would otherwise reach the SDK's callers as ValueError or
    RecursionError, which no handler of a failed remote call catches.
    """

    def json(self, **kwargs: typing.Any) -> typing.Any:
        try:
            return super().json(**kwargs)
        except (ValueError, RecursionError) as exc:
            raise InvalidRemoteAnswer(f"{type(exc).__name__}: {exc}"[:500]) from None


class PinnedTransport(httpx.BaseTransport):
    """httpx transport whose connections go only to checked addresses.

    ``pinned`` maps a hostname to the addresses ``check_url`` approved for it;
    connections to that host use those addresses without resolving it again,
    directly or through a CONNECT tunnel of the environment's proxy.
    Response bodies are read unencoded, up to ``max_response_bytes`` and, for
    JSON clients, ``max_response_values`` values. A request made outside any
    ``remote_call_limits`` block gets a budget of its own: ``REMOTE_TIMEOUT``
    for the whole request, and the response size.
    """

    def __init__(
        self,
        pinned: dict[str, list[str]] | None = None,
        scheme: str = "https",
        connect_by_hostname: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int | None = None,
        max_response_values: int | None = None,
    ):
        self._max_response_bytes = max_response_bytes
        self._max_response_values = max_response_values
        self._default_limits = core_client.RemoteCallLimits(
            max_requests=1,
            timeout=core_client.REMOTE_TIMEOUT,
            # The body, and room for the heads before it.
            max_bytes=(max_response_bytes or core_client.MAX_FETCH_BYTES)
            + MAX_HEAD_BYTES,
            max_values=max_response_values or core_client.MAX_RESPONSE_VALUES,
        )
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
        budgets = core_client.active_budgets() or (
            core_client.RemoteCallBudget(
                self._default_limits,
                deadline=time.monotonic() + self._default_limits.timeout,
            ),
        )
        _spend_request(budgets)
        extensions = dict(request.extensions)
        extensions["timeout"] = _timeouts_within(extensions.get("timeout"), budgets)
        # Whatever the caller asked for: an encoded answer is refused below.
        request.headers["Accept-Encoding"] = "identity"
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
            extensions=extensions,
        )
        with (
            core_client.using_budgets(budgets),
            _deadline_errors(budgets),
            _httpx_exceptions(),
        ):
            core_response = self._pool.handle_request(core_request)
        assert isinstance(core_response.stream, typing.Iterable)
        headers = httpx.Headers(core_response.headers)
        try:
            _refuse_encoded(headers)
            _refuse_declared_size(headers, self._max_response_bytes, budgets)
        except RemoteCallRefused:
            core_response.close()
            raise
        return _RemoteResponse(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_ResponseStream(
                core_response.stream,
                self._max_response_bytes,
                budgets,
                self._max_response_values,
                wire_counted=isinstance(
                    core_response.extensions.get("network_stream"), _BudgetedStream
                ),
            ),
            extensions=core_response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def transport_for(
    url: str,
    pinned: dict[str, list[str]],
    max_response_bytes: int | None = None,
    max_response_values: int | None = None,
) -> PinnedTransport:
    """The transport for a client of ``url``, whose host ``check_url`` passed.

    ``pinned`` maps that host to the addresses the check approved.
    """
    return PinnedTransport(
        pinned=pinned,
        scheme=httpx.URL(url).scheme,
        connect_by_hostname=bool(
            settings.WALDUR_CORE.get("OUTBOUND_PROXY_CONNECT_BY_HOSTNAME", False)
        ),
        max_response_bytes=max_response_bytes,
        max_response_values=max_response_values,
    )
