"""The outbound address check and the pinned transport behind remote Waldur clients."""

import gzip
import os
import socket
import ssl
import threading
from unittest import mock

import httpcore
import httpx
from django.test import SimpleTestCase, override_settings

from waldur_core.core.client import (
    ClientValidationError,
    check_address,
    check_url,
    fetch_url,
    get_api_root,
    get_waldur_client,
    is_internal_address,
)
from waldur_core.core.metadata import WaldurCore
from waldur_core.core.pinned_transport import (
    MAX_ADDRESSES_TRIED,
    AddressCheckingBackend,
)

DEFAULT_BLACKLIST = WaldurCore.__fields__["SUBNET_BLACKLIST"].default

# Each of these got past the check before: the IPv6 forms are never "in" an
# IPv4 network, and 0.0.0.0, :: and 100.64/10 were not on the list.
BYPASSES = [
    "0.0.0.0",
    "0.1.2.3",
    "::",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:10.0.0.1",
    "::ffff:0.0.0.0",
    "::ffff:0:127.0.0.1",  # IPv4-translated
    "::127.0.0.1",  # IPv4-compatible
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
    "64:ff9b:1::a00:1",  # local-use NAT64
    "2002:7f00:1::",  # 6to4 of 127.0.0.1
    "2002:a9fe:a9fe::1",  # 6to4 of 169.254.169.254
    "2002:a00:1::",  # 6to4 of 10.0.0.1
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo, client 127.0.0.1
    "100.64.0.1",
    "100.127.255.254",
    "198.18.0.1",
    "192.0.0.170",
    "255.255.255.255",
    "224.0.0.1",
    "ff02::1",
    "fe80::1%1",
]

# Loopback, link-local, unspecified and multicast, plain and embedded in IPv6:
# refused by the default list, open when an operator empties it.
HOST_LOCAL = [
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "0.0.0.0",
    "::",
    "169.254.169.254",
    "fe80::1",
    "224.0.0.1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:0.0.0.0",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
    "2002:7f00:1::",
    "2002:a9fe:a9fe::1",
    "::127.0.0.1",
]

PUBLIC = [
    "8.8.8.8",
    "1.1.1.1",
    "2606:4700:4700::1111",
    "::ffff:8.8.8.8",
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
    "2002:808:808::1",  # 6to4 of 8.8.8.8
]


def dns(mapping):
    """Patch DNS: each host maps to a list of addresses, or to a list of
    answers that successive lookups return in turn."""
    calls = []

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        answer = mapping[host]
        if answer and isinstance(answer[0], list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port or 0),
            )
            for address in answer
        ]

    patcher = mock.patch("socket.getaddrinfo", side_effect=getaddrinfo)
    patcher.calls = calls
    return patcher


PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def proxy_env(**variables):
    """Set exactly these proxy variables (lower case); patch.dict restores the rest."""
    patcher = mock.patch.dict(os.environ)

    class Env:
        def __enter__(self):
            patcher.start()
            for name in PROXY_VARIABLES:
                os.environ.pop(name, None)
                os.environ.pop(name.upper(), None)
            os.environ.update(variables)

        def __exit__(self, *args):
            patcher.stop()

    return Env()


class FakeServer:
    """One TCP connection whose far end answers one HTTP request.

    The test patches socket.create_connection to hand out this connection, so
    it can see which address the client meant to reach and what it sent,
    without anything listening on that address.
    """

    def __init__(self, response=None, proxy_answer=None, tls=False):
        """With ``proxy_answer``, act as a proxy first: read a CONNECT head and
        answer it; with ``tls``, then record the TLS ClientHello and hang up."""
        self.proxy_answer = proxy_answer
        self.tls = tls
        self.connect_request = b""
        self.tls_hello = b""
        self.response = response or (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n[]"
        )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_socket.connect(listener.getsockname())
        self.server_socket, _ = listener.accept()
        listener.close()
        self.received = b""
        self.addresses = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read_head(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.server_socket.recv(65536)
            if not chunk:
                break
            data += chunk
        return data

    def _serve(self):
        try:
            if self.proxy_answer is not None:
                self.connect_request = self._read_head()
                if not self.connect_request:
                    return
                self.server_socket.sendall(self.proxy_answer)
                if not self.proxy_answer.split(b" ")[1].startswith(b"2"):
                    return
                if self.tls:
                    self.tls_hello = self.server_socket.recv(65536)
                    return
            self.received = self._read_head()
            if self.received:
                self.server_socket.sendall(self.response)
        finally:
            self.server_socket.close()

    def create_connection(self, address, *args, **kwargs):
        self.addresses.append(address)
        return self.client_socket

    def patch(self):
        return mock.patch(
            "socket.create_connection", side_effect=self.create_connection
        )

    def close(self):
        self.client_socket.close()
        self.thread.join(timeout=5)


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": DEFAULT_BLACKLIST})
class AddressCheckTest(SimpleTestCase):
    def test_bypasses_are_refused_by_default(self):
        for address in BYPASSES:
            with self.subTest(address=address):
                self.assertTrue(is_internal_address(address))
                with self.assertRaisesMessage(
                    ClientValidationError, "Access to internal IPs is not allowed"
                ):
                    check_address(address)

    def test_public_addresses_pass(self):
        for address in PUBLIC:
            with self.subTest(address=address):
                self.assertFalse(is_internal_address(address))
                check_address(address)

    def test_hostname_resolving_to_a_bypass_is_refused(self):
        for address in BYPASSES:
            with self.subTest(address=address):
                with dns({"remote.example.com": [address]}):
                    with self.assertRaisesMessage(
                        ClientValidationError, "Access to internal IPs is not allowed"
                    ):
                        check_url("https://remote.example.com/api/")

    def test_ip_literal_urls_are_refused(self):
        for url in (
            "http://0.0.0.0:8000/api/",
            "http://[::]:8000/api/",
            "http://[::ffff:127.0.0.1]:8000/api/",
            "http://[::ffff:169.254.169.254]/latest/",
            "http://[64:ff9b::a9fe:a9fe]/",
            "http://[2002:7f00:1::]/",
            "http://100.64.0.1/",
        ):
            with self.subTest(url=url):
                with self.assertRaisesMessage(
                    ClientValidationError, "Access to internal IPs is not allowed"
                ):
                    check_url(url)

    def test_one_internal_answer_refuses_the_host(self):
        with dns({"remote.example.com": ["8.8.8.8", "::ffff:127.0.0.1"]}):
            with self.assertRaises(ClientValidationError):
                check_url("https://remote.example.com/api/")

    def test_public_host_passes_and_returns_its_addresses(self):
        with dns({"remote.example.com": ["8.8.8.8", "2606:4700:4700::1111"]}):
            self.assertEqual(
                check_url("https://remote.example.com/api/"),
                ["8.8.8.8", "2606:4700:4700::1111"],
            )

    def test_unresolvable_host_is_refused(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
            with self.assertRaisesMessage(
                ClientValidationError, "Error resolving client hostname"
            ):
                check_url("https://nowhere.example.com/api/")


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": DEFAULT_BLACKLIST})
class DefaultListTest(SimpleTestCase):
    def test_default_list_refuses_host_local_addresses_in_every_encoding(self):
        for address in HOST_LOCAL:
            with self.subTest(address=address):
                self.assertTrue(is_internal_address(address))


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": []})
class EmptiedListTest(SimpleTestCase):
    def test_emptied_list_allows_everything_as_configured(self):
        for address in HOST_LOCAL + ["10.0.0.1", "192.168.1.1", "fd00::1"]:
            with self.subTest(address=address):
                self.assertFalse(is_internal_address(address))


@override_settings(
    WALDUR_CORE={
        "SUBNET_BLACKLIST": [
            network
            for network in DEFAULT_BLACKLIST
            if network not in ("127.0.0.0/8", "::1/128")
        ]
    }
)
class LocalDevelopmentTest(SimpleTestCase):
    """A developer opens loopback to reach a remote Waldur on localhost."""

    def test_opened_loopback_is_reachable(self):
        for url in ("http://localhost:8000/api/", "http://127.0.0.1:8000/api/"):
            with self.subTest(url=url):
                with dns({"localhost": ["127.0.0.1"], "127.0.0.1": ["127.0.0.1"]}):
                    self.assertEqual(check_url(url), ["127.0.0.1"])
        for address in ("::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1"):
            with self.subTest(address=address):
                self.assertFalse(is_internal_address(address))

    def test_other_ranges_stay_closed(self):
        for address in ("0.0.0.0", "169.254.169.254", "::ffff:10.0.0.1", "fe80::1"):
            with self.subTest(address=address):
                self.assertTrue(is_internal_address(address))


@override_settings(
    WALDUR_CORE={
        "SUBNET_BLACKLIST": [
            network for network in DEFAULT_BLACKLIST if network != "10.0.0.0/8"
        ]
    }
)
class OperatorOpenedRangeTest(SimpleTestCase):
    def test_opened_range_is_reachable_in_every_form(self):
        with dns({"remote.internal": ["10.1.2.3"]}):
            self.assertEqual(check_url("https://remote.internal/api/"), ["10.1.2.3"])
        self.assertFalse(is_internal_address("::ffff:10.1.2.3"))
        self.assertFalse(is_internal_address("64:ff9b::a01:203"))

    def test_other_ranges_stay_closed(self):
        for address in ("192.168.1.1", "172.16.0.1", "100.64.0.1", "127.0.0.1"):
            with self.subTest(address=address):
                self.assertTrue(is_internal_address(address))


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": DEFAULT_BLACKLIST})
class UrlShapeTest(SimpleTestCase):
    def test_refused_shapes(self):
        for url in (
            "ftp://remote.example.com/api/",
            "file:///etc/passwd",
            "gopher://remote.example.com:70/",
            "//remote.example.com/api/",
            "https://user:secret@remote.example.com/api/",
            "https://remote.example.com@8.8.8.8/api/",
            "https://remote.example.com/api/?format=json",
            "https://remote.example.com/api/?",
            "https://remote.example.com/api/#fragment",
            "https://remote.example.com\\@127.0.0.1/api/",
            "https://remote.example.com /api/",
            "https://remote.example.com/api/\n",
            "https://remote.example.com:99999/api/",
        ):
            with self.subTest(url=url):
                with dns({"remote.example.com": ["8.8.8.8"]}):
                    with self.assertRaises(ClientValidationError):
                        check_url(url)

    def test_accepted_shapes(self):
        for url in (
            "https://remote.example.com",
            "https://remote.example.com/",
            "https://remote.example.com/api/",
            "http://remote.example.com:8080/api",
            "HTTPS://REMOTE.EXAMPLE.COM/api/",
        ):
            with self.subTest(url=url):
                with dns({"remote.example.com": ["8.8.8.8"]}):
                    self.assertEqual(check_url(url), ["8.8.8.8"])

    def test_api_suffix_is_removed_not_stripped(self):
        cases = {
            "https://remote.example.com/api/": "https://remote.example.com",
            "https://remote.example.com/api": "https://remote.example.com",
            "https://remote.example.com/": "https://remote.example.com",
            "https://remote.example.com": "https://remote.example.com",
            # rstrip("/api") used to eat these hostnames' trailing letters
            "https://waldur.example.pa/api/": "https://waldur.example.pa",
            "https://remote.example.api": "https://remote.example.api",
            "https://remote.example.com/rest-api/": "https://remote.example.com/rest-api",
            "https://remote.example.com/waldur/api/": "https://remote.example.com/waldur",
        }
        for api_url, root in cases.items():
            with self.subTest(api_url=api_url):
                self.assertEqual(get_api_root(api_url), root)

    def test_client_uses_api_root_as_base_url(self):
        with dns({"waldur.example.pa": ["8.8.8.8"]}):
            client = get_waldur_client("https://waldur.example.pa/api/", "token")
        self.assertEqual(client._base_url, "https://waldur.example.pa")


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": DEFAULT_BLACKLIST})
class PinnedTransportTest(SimpleTestCase):
    def setUp(self):
        self.server = FakeServer()

    def tearDown(self):
        self.server.close()

    def test_connection_goes_to_the_checked_address(self):
        # The check sees 8.8.8.8; any later lookup would answer 127.0.0.1.
        resolver = dns({"remote.example.com": [["8.8.8.8"], ["127.0.0.1"]]})
        with resolver:
            client = get_waldur_client("http://remote.example.com/api/", "secret")
            with self.server.patch():
                response = client.get_httpx_client().get("/api/customers/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])
        self.assertEqual(resolver.calls, ["remote.example.com"])
        request = self.server.received.decode()
        self.assertTrue(request.startswith("GET /api/customers/ HTTP/1.1\r\n"))
        self.assertIn("\r\nHost: remote.example.com\r\n", request)
        self.assertIn("\r\nAuthorization: Token secret\r\n", request)

    def test_ipv6_literal_is_pinned(self):
        client = get_waldur_client("http://[2606:4700:4700::1111]:8080/api/", "t")
        with self.server.patch():
            response = client.get_httpx_client().get("/api/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.addresses, [("2606:4700:4700::1111", 8080)])

    def test_other_host_is_resolved_and_checked_at_connect_time(self):
        with dns(
            {"remote.example.com": ["8.8.8.8"], "other.example.com": ["10.0.0.1"]}
        ):
            client = get_waldur_client("http://remote.example.com/api/", "t")
            with self.server.patch():
                with self.assertRaisesMessage(
                    httpx.ConnectError, "Access to internal IPs is not allowed"
                ):
                    client.get_httpx_client().get("http://other.example.com/api/")
        self.assertEqual(self.server.addresses, [])

    def test_rebinding_between_lookups_is_refused(self):
        backend = AddressCheckingBackend()
        with dns({"rebind.example.com": [["8.8.8.8"], ["169.254.169.254"]]}):
            check_url("http://rebind.example.com/")
            with self.server.patch():
                with self.assertRaises(httpcore.ConnectError):
                    backend.connect_tcp("rebind.example.com", 80)
        self.assertEqual(self.server.addresses, [])

    def test_redirect_is_not_followed(self):
        self.server.close()
        self.server = FakeServer(
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        with dns({"remote.example.com": ["8.8.8.8"]}):
            client = get_waldur_client("http://remote.example.com/api/", "t")
            with self.server.patch():
                response = client.get_httpx_client().get("/api/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])

    def test_tls_uses_the_hostname_for_sni_and_verification(self):
        seen = {}

        def start_tls(ssl_context, server_hostname=None, timeout=None):
            seen["server_hostname"] = server_hostname
            seen["check_hostname"] = ssl_context.check_hostname
            seen["verify_mode"] = ssl_context.verify_mode
            raise httpcore.ConnectError("stop here")

        stream = mock.Mock()
        stream.start_tls.side_effect = start_tls
        with dns({"remote.example.com": ["8.8.8.8"]}):
            client = get_waldur_client("https://remote.example.com/api/", "t")
            with mock.patch.object(
                httpcore.SyncBackend, "connect_tcp", return_value=stream
            ) as connect_tcp:
                with self.assertRaises(httpx.ConnectError):
                    client.get_httpx_client().get("/api/")

        self.assertEqual(connect_tcp.call_args.args, ("8.8.8.8", 443))
        self.assertEqual(seen["server_hostname"], "remote.example.com")
        self.assertTrue(seen["check_hostname"])
        self.assertEqual(seen["verify_mode"], ssl.CERT_REQUIRED)

    def test_fetch_url_refuses_internal_address(self):
        with dns({"img.example.com": ["169.254.169.254"]}), self.server.patch():
            with self.assertRaisesMessage(
                httpx.ConnectError, "Access to internal IPs is not allowed"
            ):
                fetch_url("http://img.example.com/media/image/")
        self.assertEqual(self.server.addresses, [])

    def test_fetch_url_is_pinned(self):
        resolver = dns({"img.example.com": [["8.8.8.8"], ["127.0.0.1"]]})
        with resolver, self.server.patch():
            response = fetch_url("http://img.example.com/media/image/?size=small")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])
        self.assertEqual(resolver.calls, ["img.example.com"])

    def test_fetch_url_stops_reading_past_the_size_cap(self):
        self.server.close()
        self.server = FakeServer(
            b"HTTP/1.1 200 OK\r\nContent-Length: 64\r\n\r\n" + b"x" * 64
        )
        with dns({"img.example.com": ["8.8.8.8"]}), self.server.patch():
            with mock.patch("waldur_core.core.client.MAX_FETCH_BYTES", 16):
                with self.assertRaises(httpx.ReadError):
                    fetch_url("http://img.example.com/media/image/")

    def test_malformed_url_is_a_connect_error_for_fetch_url(self):
        for url in ("http://[::1/", "http://[not-an-ip]/"):
            with self.subTest(url=url):
                with self.assertRaises(httpx.ConnectError):
                    fetch_url(url)
                with self.assertRaises(ClientValidationError):
                    check_url(url)

    def test_fetch_url_asks_for_and_accepts_an_unencoded_answer(self):
        with dns({"img.example.com": ["8.8.8.8"]}), self.server.patch():
            response = fetch_url("http://img.example.com/media/image.svg")
        response.raise_for_status()
        self.assertEqual(response.content, b"[]")
        self.assertIn(b"\r\nAccept-Encoding: identity\r\n", self.server.received)

    def test_fetch_url_refuses_an_encoded_answer(self):
        body = b"\0" * (1024 * 1024)
        for encoding, payload in (
            ("gzip", gzip.compress(body)),
            # Chained encodings would be decompressed twice, in full.
            ("gzip, gzip", gzip.compress(gzip.compress(body))),
        ):
            with self.subTest(encoding=encoding):
                self.use_server(
                    FakeServer(
                        b"HTTP/1.1 200 OK\r\nContent-Type: image/svg+xml\r\n"
                        b"Content-Encoding: " + encoding.encode() + b"\r\n"
                        b"Content-Length: "
                        + str(len(payload)).encode()
                        + b"\r\n\r\n"
                        + payload
                    )
                )
                with dns({"img.example.com": ["8.8.8.8"]}), self.server.patch():
                    with self.assertRaises(httpx.DecodingError):
                        fetch_url("http://img.example.com/media/image.svg")

    def test_a_few_addresses_are_tried_within_one_timeout(self):
        attempts = []

        def refuse(address, *args, **kwargs):
            attempts.append(address)
            raise ConnectionRefusedError

        addresses = [f"8.8.8.{n}" for n in range(1, 7)]
        backend = AddressCheckingBackend({"remote.example.com": addresses})
        with proxy_env(), mock.patch("socket.create_connection", side_effect=refuse):
            with self.assertRaises(httpcore.ConnectError):
                backend.connect_tcp("remote.example.com", 80, timeout=5)
        self.assertEqual(len(attempts), MAX_ADDRESSES_TRIED)

    def use_server(self, server):
        self.server.close()
        self.server = server

    def test_client_has_a_timeout_and_no_redirects(self):
        with dns({"remote.example.com": ["8.8.8.8"]}):
            client = get_waldur_client("https://remote.example.com/api/", "t")
        httpx_client = client.get_httpx_client()
        self.assertFalse(httpx_client.follow_redirects)
        self.assertIsNotNone(httpx_client.timeout.read)
        self.assertIsNotNone(httpx_client.timeout.connect)


TUNNEL_OK = b"HTTP/1.1 200 Connection established\r\n\r\n"


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": DEFAULT_BLACKLIST})
class EnvironmentProxyTest(SimpleTestCase):
    """An egress proxy from the environment is used through a CONNECT tunnel
    to the checked address, so the pin holds behind the proxy too."""

    HTTP_PROXY = {"http_proxy": "http://proxy.example.net:3128"}
    HTTPS_PROXY = {"https_proxy": "http://proxy.example.net:3128"}

    def setUp(self):
        self.server = FakeServer(proxy_answer=TUNNEL_OK)

    def tearDown(self):
        self.server.close()

    def use(self, server):
        self.server.close()
        self.server = server

    def test_http_target_is_tunnelled_to_the_checked_address(self):
        # The check sees 8.8.8.8; any later lookup would answer 127.0.0.1.
        resolver = dns({"remote.example.com": [["8.8.8.8"], ["127.0.0.1"]]})
        with proxy_env(**self.HTTP_PROXY), resolver:
            client = get_waldur_client("http://remote.example.com/api/", "secret")
            with self.server.patch():
                response = client.get_httpx_client().get("/api/customers/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(resolver.calls, ["remote.example.com"])
        self.assertEqual(self.server.addresses, [("proxy.example.net", 3128)])
        self.assertTrue(
            self.server.connect_request.startswith(b"CONNECT 8.8.8.8:80 HTTP/1.1\r\n")
        )
        request = self.server.received.decode()
        self.assertTrue(request.startswith("GET /api/customers/ HTTP/1.1\r\n"))
        self.assertIn("\r\nHost: remote.example.com\r\n", request)

    def test_https_target_keeps_the_hostname_for_tls(self):
        self.use(FakeServer(proxy_answer=TUNNEL_OK, tls=True))
        with proxy_env(**self.HTTPS_PROXY), dns({"remote.example.com": ["8.8.8.8"]}):
            client = get_waldur_client("https://remote.example.com/api/", "t")
            with self.server.patch():
                with self.assertRaises(httpx.ConnectError):
                    client.get_httpx_client().get("/api/")
        self.assertTrue(self.server.connect_request.startswith(b"CONNECT 8.8.8.8:443 "))
        # The TLS ClientHello through the tunnel names the host for SNI.
        self.assertIn(b"remote.example.com", self.server.tls_hello)

    def test_ipv6_address_is_bracketed(self):
        with proxy_env(**self.HTTP_PROXY):
            with dns({"remote.example.com": ["2606:4700:4700::1111"]}):
                client = get_waldur_client("http://remote.example.com/api/", "t")
                with self.server.patch():
                    client.get_httpx_client().get("/api/")
        self.assertTrue(
            self.server.connect_request.startswith(
                b"CONNECT [2606:4700:4700::1111]:80 HTTP/1.1"
            )
        )

    def test_proxy_credentials_are_sent(self):
        with proxy_env(http_proxy="http://user:pa%40ss@proxy.example.net:3128"):
            with dns({"remote.example.com": ["8.8.8.8"]}), self.server.patch():
                fetch_url("http://remote.example.com/media/image/")
        self.assertIn(
            b"\r\nProxy-Authorization: Basic dXNlcjpwYUBzcw==\r\n",
            self.server.connect_request,
        )

    def test_refused_tunnel_is_a_proxy_error(self):
        self.use(FakeServer(proxy_answer=b"HTTP/1.1 403 Forbidden\r\n\r\n"))
        with proxy_env(**self.HTTP_PROXY), dns({"remote.example.com": ["8.8.8.8"]}):
            client = get_waldur_client("http://remote.example.com/api/", "t")
            with self.server.patch():
                with self.assertRaisesMessage(httpx.ProxyError, "403"):
                    client.get_httpx_client().get("/api/")

    def test_refused_target_never_reaches_the_proxy(self):
        with proxy_env(**self.HTTP_PROXY), self.server.patch():
            with dns({"remote.example.com": ["10.0.0.1"]}):
                with self.assertRaisesMessage(
                    ClientValidationError, "Access to internal IPs is not allowed"
                ):
                    get_waldur_client("http://remote.example.com/api/", "t")
            with self.assertRaisesMessage(
                httpx.ConnectError, "Access to internal IPs is not allowed"
            ):
                fetch_url("http://[::ffff:169.254.169.254]/latest/meta-data/")
        self.assertEqual(self.server.addresses, [])

    def test_other_host_is_checked_before_the_proxy(self):
        resolver = dns(
            {"remote.example.com": ["8.8.8.8"], "other.example.com": ["127.0.0.1"]}
        )
        with proxy_env(**self.HTTP_PROXY), resolver:
            client = get_waldur_client("http://remote.example.com/api/", "t")
            with self.server.patch():
                with self.assertRaisesMessage(
                    httpx.ConnectError, "Access to internal IPs is not allowed"
                ):
                    client.get_httpx_client().get("http://other.example.com/")
        self.assertEqual(self.server.addresses, [])

    def test_no_proxy_host_connects_directly_and_pinned(self):
        self.use(FakeServer())
        resolver = dns({"remote.example.com": [["8.8.8.8"], ["127.0.0.1"]]})
        with proxy_env(no_proxy="example.com", **self.HTTP_PROXY), resolver:
            client = get_waldur_client("http://remote.example.com/api/", "t")
            with self.server.patch():
                response = client.get_httpx_client().get("/api/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])

    def test_proxy_for_another_scheme_is_not_used(self):
        self.use(FakeServer())
        with proxy_env(**self.HTTPS_PROXY):
            with dns({"remote.example.com": ["8.8.8.8"]}), self.server.patch():
                response = fetch_url("http://remote.example.com/media/image/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])

    def test_fetch_url_is_tunnelled(self):
        with proxy_env(**self.HTTP_PROXY), dns({"img.example.com": ["8.8.8.8"]}):
            with self.server.patch():
                response = fetch_url("http://img.example.com/media/image/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.addresses, [("proxy.example.net", 3128)])
        self.assertTrue(self.server.connect_request.startswith(b"CONNECT 8.8.8.8:80 "))

    @override_settings(
        WALDUR_CORE={
            "SUBNET_BLACKLIST": DEFAULT_BLACKLIST,
            "OUTBOUND_PROXY_CONNECT_BY_HOSTNAME": True,
        }
    )
    def test_connect_by_hostname_is_opt_in(self):
        with proxy_env(**self.HTTP_PROXY), dns({"remote.example.com": ["8.8.8.8"]}):
            client = get_waldur_client("http://remote.example.com/api/", "t")
            with self.server.patch():
                client.get_httpx_client().get("/api/")
        self.assertTrue(
            self.server.connect_request.startswith(b"CONNECT remote.example.com:80 ")
        )
