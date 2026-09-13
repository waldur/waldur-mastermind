"""The web shell server: ghostty-web in the browser, `waldur shell` in a PTY.

Started by `waldur web_shell` as its own process, never inside the API
workers. The API mints tickets (tickets.py); this server redeems them.

Wire protocol on /webshell/ws:
  client -> server  text   {"type": "auth", "ticket": ..., "cols": N, "rows": N}  (first frame)
  client -> server  text   {"type": "resize", "cols": N, "rows": N}
  client -> server  binary keystrokes, written to the PTY as-is
  server -> client  text   {"type": "ready", "user": ...}
  server -> client  binary PTY output, as-is
Control and data travel in different frame types, so pasted JSON is never
mistaken for a control message.
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import socket
import struct
import sys
import termios
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web
from constance import config
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import close_old_connections
from rest_framework.authtoken.models import Token

from waldur_core.web_shell import assets, tickets

# The child execs immediately after pty.fork(), so the usual hazard of forking
# a multi-threaded process (inherited locks) does not apply here.
warnings.filterwarnings("ignore", message=".*fork.*", category=DeprecationWarning)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SHELL_ARGV = ["waldur", "shell"]
AUTH_TIMEOUT = 10
IDLE_TIMEOUT = int(os.environ.get("WALDUR_WEB_SHELL_IDLE_TIMEOUT", "900"))
TRANSCRIPT_DIR = os.environ.get("WALDUR_WEB_SHELL_TRANSCRIPT_DIR")
ACCESS_CHECK_INTERVAL = int(
    os.environ.get("WALDUR_WEB_SHELL_ACCESS_CHECK_INTERVAL", "30")
)
# Consecutive failed access checks (e.g. the database is unreachable) after
# which a session is closed rather than left running unchecked.
MAX_ACCESS_CHECK_FAILURES = 3
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

CSP = "; ".join(
    [
        "default-src 'none'",
        # ghostty-web compiles its VT parser from WASM in the browser.
        "script-src 'self' 'wasm-unsafe-eval'",
        "connect-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

CLOSE_SHELL_EXITED = 4000
CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403
CLOSE_IDLE = 4408
CLOSE_DUPLICATE = 4409

logger = logging.getLogger(__name__)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def allowed_hosts() -> set[str]:
    # A host allowlist guards against DNS rebinding: a hostile domain resolved
    # to 127.0.0.1 would otherwise reach the server under its own name.
    hosts = set(LOOPBACK_HOSTS)
    hostname = urlsplit(settings.WALDUR_CORE["WEB_SHELL_URL"]).hostname
    if hostname:
        hosts.add(hostname.lower())
    # Extra names a reverse proxy may forward under, e.g. an internal API
    # hostname. Browsers must still come from the WEB_SHELL_URL origin.
    for host in os.environ.get("WALDUR_WEB_SHELL_ALLOWED_HOSTS", "").split(","):
        if host.strip():
            hosts.add(host.strip().lower())
    return hosts


def normalized_origin(url: str | None) -> str | None:
    """scheme://host[:port] of a URL, without a default port, or None."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    default_port = 443 if parts.scheme == "https" else 80
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{suffix}"


class TicketStore:
    """Remembers redeemed ticket nonces until they would have expired anyway.

    In-process only, which is enough for one server. Several would need the
    shared Django cache (cache.add) instead.
    """

    def __init__(self):
        self._used: dict[str, float] = {}

    def claim(self, nonce: str) -> bool:
        now = time.monotonic()
        self._used = {n: expiry for n, expiry in self._used.items() if expiry > now}
        if nonce in self._used:
            return False
        self._used[nonce] = now + tickets.MAX_AGE
        return True


def get_staff_user(user_uuid: str):
    return (
        get_user_model()
        .objects.filter(uuid=user_uuid, is_active=True, is_staff=True)
        .first()
    )


def run_orm(func, *args):
    """Call an ORM function in a worker thread with a usable connection.

    Django closes stale or broken connections only around HTTP requests. This
    long-lived process reuses worker threads, so do it around each call, or a
    connection dropped by a database restart would fail every later call.
    """
    close_old_connections()
    try:
        return func(*args)
    finally:
        close_old_connections()


def check_access(user_pk: int, token_digest: str | None) -> str | None:
    """Why the session has to end now, or None while it may continue.

    The ticket is checked once, at connect. This re-checks what it stood for:
    an active staff account and, when the link was requested with a Waldur API
    token, that the token still exists. Logging out deletes the token and
    signing in again replaces it, so either ends the session.
    """
    # all_objects: the active manager hides deactivated accounts, which are
    # exactly the ones this has to catch.
    user = get_user_model().all_objects.filter(pk=user_pk).first()
    if user is None or not user.is_active:
        return "Account deactivated"
    if not user.is_staff:
        return "No longer staff"
    if token_digest:
        key = Token.objects.filter(user=user).values_list("key", flat=True).first()
        if key is None or not tickets.token_matches(key, token_digest):
            return "Signed out of Waldur"
    return None


def primary_ip() -> str | None:
    # The address this host uses for outbound traffic. Connecting a UDP socket
    # only selects a route; nothing is sent.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return None


def describe_environment() -> dict:
    """Which deployment the shell is attached to, for the page header.

    Sent only after a ticket is accepted, so anonymous visitors learn nothing
    about the host or the database.
    """
    database = settings.DATABASES["default"]
    return {
        "site_name": config.SITE_NAME,
        "portal": urlsplit(config.HOMEPORT_URL).netloc or config.HOMEPORT_URL,
        "host": socket.gethostname(),
        "ip": primary_ip(),
        "database": "{}@{}:{}".format(
            database["NAME"],
            database.get("HOST") or "localhost",
            database.get("PORT") or 5432,
        ),
    }


def describe_user(user) -> dict:
    return {
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
    }


def mint_link(username: str) -> str:
    user = (
        get_user_model()
        .objects.filter(username=username, is_active=True, is_staff=True)
        .first()
    )
    if user is None:
        raise CommandError(f"No active staff user {username!r}.")
    return tickets.build_url(tickets.mint(user))


# HTTP

TICKETS = web.AppKey("tickets", TicketStore)
SESSIONS = web.AppKey("sessions", dict)
ALLOWED_HOSTS = web.AppKey("allowed_hosts", set)
PUBLIC_ORIGIN = web.AppKey("public_origin", str)


def hostname_of(host_header: str | None) -> str | None:
    if not host_header:
        return None
    return (urlsplit("//" + host_header).hostname or "").lower() or None


def origin_allowed(request: web.Request) -> bool:
    # Compared with the configured public origin rather than the Host header:
    # a reverse proxy may forward under an internal hostname.
    origin = normalized_origin(request.headers.get("Origin"))
    return origin is not None and origin == request.app[PUBLIC_ORIGIN]


@web.middleware
async def guard(request: web.Request, handler):
    if hostname_of(request.host) not in request.app[ALLOWED_HOSTS]:
        return web.Response(status=403, text="Forbidden host\n")
    response = await handler(request)
    if not response.prepared:
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def redirect_to_index(request: web.Request):
    raise web.HTTPFound("/webshell/")


async def index(request: web.Request):
    response = web.FileResponse(STATIC_DIR / "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


# Terminal


def set_winsize(fd: int, rows: int, cols: int):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def clamp(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class PtySession:
    def __init__(
        self,
        ws: web.WebSocketResponse,
        user,
        remote: str,
        cols,
        rows,
        token_digest: str | None = None,
    ):
        self.ws = ws
        self.user = user
        self.token_digest = token_digest
        self.remote = remote
        self.cols = clamp(cols, 2, 1000, 80)
        self.rows = clamp(rows, 2, 1000, 24)
        self.pid: int | None = None
        self.fd: int | None = None
        self.output: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self.reading = False
        self.eof = False
        self.started = time.monotonic()
        self.last_activity = self.started
        self.transcript = None

    def spawn(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                set_winsize(0, self.rows, self.cols)
                env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
                os.execvpe(SHELL_ARGV[0], SHELL_ARGV, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        os.set_blocking(fd, False)
        if TRANSCRIPT_DIR:
            Path(TRANSCRIPT_DIR).mkdir(parents=True, exist_ok=True)
            name = f"{time.strftime('%Y%m%dT%H%M%S')}-{self.user.username}-{pid}.log"
            # Unbuffered, so the record survives the process being killed.
            self.transcript = open(Path(TRANSCRIPT_DIR) / name, "ab", buffering=0)  # noqa: SIM115

    def _resume_reading(self):
        if not self.reading and not self.eof:
            asyncio.get_running_loop().add_reader(self.fd, self._on_readable)
            self.reading = True

    def _pause_reading(self):
        if self.reading:
            asyncio.get_running_loop().remove_reader(self.fd)
            self.reading = False

    def _on_readable(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO once the child exits and the slave side closes
            data = b""
        if not data:
            self.eof = True
            self._pause_reading()
            self.output.put_nowait(None)
            return
        self.last_activity = time.monotonic()
        if self.transcript:
            self.transcript.write(data)
        self.output.put_nowait(data)
        if self.output.full():
            # Browser is slower than the shell: stop reading until it catches up.
            self._pause_reading()

    async def _pump_output(self):
        while True:
            data = await self.output.get()
            if data is None:
                await self.ws.close(code=CLOSE_SHELL_EXITED, message=b"Shell exited")
                return
            await self.ws.send_bytes(data)
            if self.output.empty():
                self._resume_reading()

    async def _watch_idle(self):
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - self.last_activity > IDLE_TIMEOUT:
                await self.ws.close(code=CLOSE_IDLE, message=b"Idle timeout")
                return

    async def _watch_access(self):
        failures = 0
        while True:
            await asyncio.sleep(ACCESS_CHECK_INTERVAL)
            try:
                reason = await asyncio.to_thread(
                    run_orm, check_access, self.user.pk, self.token_digest
                )
            except Exception:
                failures += 1
                logger.exception(
                    "Web shell access check failed for user %s (%s in a row)",
                    self.user.username,
                    failures,
                )
                if failures < MAX_ACCESS_CHECK_FAILURES:
                    continue
                reason = "Access could not be verified"
            else:
                failures = 0
            if reason:
                logger.info(
                    "Web shell session for user %s revoked: %s",
                    self.user.username,
                    reason,
                )
                await self.ws.close(code=CLOSE_REVOKED, message=reason.encode())
                return

    async def _write_input(self, data: bytes):
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                await asyncio.sleep(0.01)
                continue
            except OSError:
                return
            view = view[written:]

    def _handle_control(self, text: str):
        try:
            message = json.loads(text)
        except ValueError:
            return
        if isinstance(message, dict) and message.get("type") == "resize":
            self.cols = clamp(message.get("cols"), 2, 1000, self.cols)
            self.rows = clamp(message.get("rows"), 2, 1000, self.rows)
            set_winsize(self.fd, self.rows, self.cols)

    async def run(self):
        tasks: list[asyncio.Task] = []
        # Everything after spawn() sits inside try/finally, so a failure while
        # starting (the browser gone, a database error) still ends the shell.
        try:
            self.spawn()
            logger.info(
                "Web shell session started for user %s from %s, pid %s",
                self.user.username,
                self.remote,
                self.pid,
            )
            environment = await asyncio.to_thread(run_orm, describe_environment)
            await self.ws.send_json(
                {
                    "type": "ready",
                    "user": describe_user(self.user),
                    "environment": environment,
                }
            )
            self._resume_reading()
            tasks = [
                asyncio.create_task(self._pump_output()),
                asyncio.create_task(self._watch_idle()),
                asyncio.create_task(self._watch_access()),
            ]
            async for message in self.ws:
                self.last_activity = time.monotonic()
                if message.type == WSMsgType.BINARY:
                    await self._write_input(message.data)
                elif message.type == WSMsgType.TEXT:
                    self._handle_control(message.data)
        finally:
            for task in tasks:
                task.cancel()
            await self._close()

    async def _close(self):
        self._pause_reading()
        if self.fd is not None:
            os.close(self.fd)
        exit_code = await self._terminate()
        if self.transcript:
            self.transcript.close()
        logger.info(
            "Web shell session ended for user %s, pid %s, after %.0fs, exit %s",
            self.user.username,
            self.pid,
            time.monotonic() - self.started,
            exit_code,
        )

    async def _terminate(self):
        if self.pid is None:
            return None
        # pty.fork() makes the child a session leader, so its pid is also the
        # process group: this reaches anything the shell started too.
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                except ChildProcessError:
                    return None
                if pid:
                    return os.waitstatus_to_exitcode(status)
                await asyncio.sleep(0.1)
        return None


async def authenticate(app: web.Application, message):
    if message.type != WSMsgType.TEXT:
        return None, None, {}
    try:
        hello = json.loads(message.data)
    except ValueError:
        return None, None, {}
    if not isinstance(hello, dict) or hello.get("type") != "auth":
        return None, None, {}
    ticket = tickets.load(str(hello.get("ticket") or ""))
    if ticket is None or not app[TICKETS].claim(ticket.nonce):
        return None, None, hello
    user = await asyncio.to_thread(run_orm, get_staff_user, ticket.user_uuid)
    return user, ticket, hello


async def terminal(request: web.Request):
    if not origin_allowed(request):
        return web.Response(status=403, text="Bad origin\n")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)

    try:
        first = await asyncio.wait_for(ws.receive(), AUTH_TIMEOUT)
    except TimeoutError:
        await ws.close(code=CLOSE_UNAUTHORIZED, message=b"No ticket")
        return ws

    user, ticket, hello = await authenticate(request.app, first)
    if user is None:
        logger.warning("Web shell ticket rejected from %s", request.remote)
        await ws.close(code=CLOSE_UNAUTHORIZED, message=b"Invalid ticket")
        return ws

    # The link may have been requested moments before signing out.
    reason = await asyncio.to_thread(
        run_orm, check_access, user.pk, ticket.token_digest
    )
    if reason:
        await ws.close(code=CLOSE_REVOKED, message=reason.encode())
        return ws

    sessions = request.app[SESSIONS]
    if user.pk in sessions:
        await ws.close(code=CLOSE_DUPLICATE, message=b"Shell already open")
        return ws

    sessions[user.pk] = ws
    try:
        session = PtySession(
            ws,
            user,
            request.remote,
            hello.get("cols"),
            hello.get("rows"),
            token_digest=ticket.token_digest,
        )
        await session.run()
    finally:
        sessions.pop(user.pk, None)
    return ws


async def close_sessions(app: web.Application):
    for ws in list(app[SESSIONS].values()):
        await ws.close(code=1001, message=b"Server shutting down")


def build_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app[TICKETS] = TicketStore()
    app[SESSIONS] = {}
    app[ALLOWED_HOSTS] = allowed_hosts()
    app[PUBLIC_ORIGIN] = normalized_origin(settings.WALDUR_CORE["WEB_SHELL_URL"])
    app.router.add_get("/", redirect_to_index)
    app.router.add_get("/webshell", redirect_to_index)
    app.router.add_get("/webshell/", index)
    app.router.add_get("/webshell/ws", terminal)
    app.router.add_static("/webshell/static/", STATIC_DIR)
    app.router.add_static("/webshell/vendor/ghostty-web/", assets.assets_dir())
    app.on_shutdown.append(close_sessions)
    return app


def serve(host: str, port: int):
    configure_logging()
    app = build_app()
    logger.info(
        "Web shell serving on %s:%s for %s (allowed hosts: %s)",
        host,
        port,
        settings.WALDUR_CORE["WEB_SHELL_URL"],
        ", ".join(sorted(app[ALLOWED_HOSTS])),
    )
    web.run_app(app, host=host, port=port, print=None)
