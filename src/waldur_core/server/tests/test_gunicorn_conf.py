"""The image's gunicorn configuration, loaded by the gunicorn version in uv.lock.

``gunicorn.conf.py`` leans on gunicorn internals: its access logger subclasses
``gunicorn.glogging.Logger`` and rewrites atoms by name to keep the Matrix
appservice token out of the access log, and it switches off defaults that newer
gunicorn releases turned on. Nothing in gunicorn's public API promises any of
that, so these tests load the file the way the container does, with the command
line from the image's ``mastermind`` script and the flags waldur-helm passes,
and run its logger and gunicorn's request parsing against that configuration.
A bump that renames an atom or setting fails here. The error pages gunicorn
writes itself are read from a real gunicorn started with the image's command
line and conf file, since only the running server shows that the policy reaches
them. Its arbiter runs the conf's hooks, post_fork included, but the trivial app
served there never loads Django, so post_fork's closing of the connections a
preloaded mastermind opens is still a container-level check.
"""

import datetime
import http.client
import logging
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import tempfile
import types
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from django.utils.csp import build_policy
from gunicorn.app.wsgiapp import WSGIApplication
from gunicorn.glogging import Logger
from gunicorn.http import wsgi
from gunicorn.http.parser import RequestParser

ROOTFS = pathlib.Path(__file__).parents[4] / "docker" / "rootfs"
IMAGE_CONF_PATH = "/etc/waldur/gunicorn.conf.py"
CONF_FILE = ROOTFS / IMAGE_CONF_PATH.lstrip("/")
MASTERMIND_SCRIPT = ROOTFS / "usr" / "local" / "bin" / "mastermind"

# Every flag waldur-helm can render into GUNICORN_CMD_ARGS
# (waldur-helm: waldur/templates/_helpers.tpl, "waldur.gunicornCmdArgs").
HELM_CMD_ARGS = (
    "--timeout 61 --graceful-timeout 31 --workers 3 --keep-alive 7 "
    "--max-requests 501 --max-requests-jitter 21"
)

CLIENT = ("203.0.113.7", 40000)
SERVER = ("10.0.0.2", 8080)

# Served in place of mastermind, with a policy of its own, so that its
# responses can be told from the error pages gunicorn writes itself.
APP_POLICY = "default-src 'self'"
TRIVIAL_APP = f'''\
def app(environ, start_response):
    start_response(
        "200 OK",
        [("Content-Type", "text/plain"), ("Content-Security-Policy", "{APP_POLICY}")],
    )
    return [b"ok"]
'''


def image_argv():
    """The gunicorn command line the image runs, pointed at the repo's conf file."""
    for line in MASTERMIND_SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.startswith("gunicorn "):
            return [
                str(CONF_FILE) if arg == IMAGE_CONF_PATH else arg
                for arg in shlex.split(line)
            ]
    raise AssertionError(f"no gunicorn command in {MASTERMIND_SCRIPT}")


def load_image_config(cmd_args=""):
    """Load the configuration as the container's gunicorn does, without serving."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GUNICORN_")}
    environ["GUNICORN_CMD_ARGS"] = cmd_args
    cwd = os.getcwd()
    try:
        with (
            mock.patch.object(sys, "argv", image_argv()),
            mock.patch.object(sys, "path", list(sys.path)),
            mock.patch.dict(sys.modules),  # the conf file loads as "__config__"
            mock.patch.dict(os.environ, environ, clear=True),
        ):
            # An argument gunicorn does not accept ends in SystemExit here.
            return WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]").cfg
    finally:
        os.chdir(cwd)  # Application.chdir() moves to cfg.chdir


def wsgi_environ(cfg, raw_request):
    """The WSGI environ gunicorn builds for ``raw_request`` under ``cfg``."""
    req = next(iter(RequestParser(cfg, iter([raw_request]), CLIENT)))
    _, environ = wsgi.create(req, mock.Mock(), CLIENT, SERVER, cfg)
    return environ


class ImageCommandLineTest(SimpleTestCase):
    def test_the_image_command_line_loads_the_conf_file(self):
        cfg = load_image_config()

        self.assertEqual(cfg.workers, 4)
        self.assertTrue(cfg.preload_app)
        self.assertEqual(cfg.max_requests, 1000)
        self.assertEqual(cfg.max_requests_jitter, 50)
        self.assertEqual(cfg.accesslog, "-")
        # Loaded from the conf file; that the arbiter calls it is not checked here.
        self.assertEqual(cfg.post_fork.__module__, "__config__")
        self.assertTrue(issubclass(cfg.logger_class, Logger))
        self.assertEqual(
            cfg.env, {"DJANGO_SETTINGS_MODULE": "waldur_core.server.settings"}
        )

    def test_every_helm_flag_is_accepted(self):
        cfg = load_image_config(HELM_CMD_ARGS)

        self.assertEqual(cfg.timeout, 61)
        self.assertEqual(cfg.graceful_timeout, 31)
        self.assertEqual(cfg.workers, 3)
        self.assertEqual(cfg.keepalive, 7)
        self.assertEqual(cfg.max_requests, 501)
        self.assertEqual(cfg.max_requests_jitter, 21)

    def test_the_control_socket_is_off(self):
        self.assertTrue(load_image_config().control_socket_disable)


class ForwarderHeadersTest(SimpleTestCase):
    """Every peer is a trusted forwarder, so no client may set WSGI path keys."""

    def test_a_client_script_name_header_does_not_reroot_the_path(self):
        environ = wsgi_environ(
            load_image_config(),
            b"GET /api/admin/ HTTP/1.1\r\nHost: localhost\r\nSCRIPT_NAME: /api\r\n\r\n",
        )

        self.assertEqual(environ["PATH_INFO"], "/api/admin/")
        self.assertEqual(environ["SCRIPT_NAME"], "")
        self.assertNotIn("HTTP_SCRIPT_NAME", environ)


class AccessLogTest(SimpleTestCase):
    def setUp(self):
        # Logger() installs its own handlers and levels on these process-wide loggers.
        for name in ("gunicorn.access", "gunicorn.error"):
            log = logging.getLogger(name)
            self.addCleanup(setattr, log, "handlers", list(log.handlers))
            self.addCleanup(setattr, log, "propagate", log.propagate)
            self.addCleanup(log.setLevel, log.level)
        cfg = load_image_config()
        self.logger = cfg.logger_class(cfg)

    def access_line(self, path, query):
        environ = {
            "REQUEST_METHOD": "PUT",
            "RAW_URI": f"{path}?{query}",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_USER_AGENT": "Synapse/1.0",
        }
        resp = types.SimpleNamespace(status="200 OK", sent=2, headers=[])
        req = types.SimpleNamespace(headers=[("X-FORWARDED-FOR", CLIENT[0])])
        with self.assertLogs("gunicorn.access", logging.INFO) as logs:
            self.logger.access(resp, req, environ, datetime.timedelta(seconds=1))
        [record] = logs.records
        return record.getMessage()

    def test_an_appservice_request_is_logged_without_its_query(self):
        line = self.access_line(
            "/_matrix/app/v1/transactions/1", "access_token=hs-secret-token"
        )

        self.assertNotIn("hs-secret-token", line)
        self.assertIn('"PUT /_matrix/app/v1/transactions/1 HTTP/1.1" 200 2', line)
        self.assertTrue(line.startswith(f"{CLIENT[0]} "))

    def test_any_other_request_keeps_its_query(self):
        line = self.access_line("/api/customers/", "page=2")

        self.assertIn('"PUT /api/customers/?page=2 HTTP/1.1" 200 2', line)


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class ErrorPageTests:
    """gunicorn's own error pages carry the policy Django sends on API responses.

    gunicorn answers these requests before the WSGI app sees them, so they are
    sent to a real gunicorn started with the image's command line and conf file.
    """

    worker_class: str

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        app_dir = pathlib.Path(tmp.name)
        (app_dir / "trivial_app.py").write_text(TRIVIAL_APP, encoding="utf-8")
        cls.log_path = app_dir / "gunicorn.log"
        cls.cfg = load_image_config()
        # gunicorn takes over a socket bound here, so no free port is guessed;
        # connections wait in its backlog until the worker is up.
        listener = socket.create_server(("127.0.0.1", 0))
        cls.address = listener.getsockname()
        argv = [
            sys.executable,
            "-m",
            "gunicorn",
            *image_argv()[1:-1],  # without the program and mastermind's app
            "--bind",
            f"fd://{listener.fileno()}",
            "--workers",
            "1",
            "--worker-class",
            cls.worker_class,
            "--pythonpath",
            str(app_dir),
            "trivial_app:app",
        ]
        environ = {k: v for k, v in os.environ.items() if not k.startswith("GUNICORN_")}
        with listener, open(cls.log_path, "wb") as log:
            cls.process = subprocess.Popen(
                argv,
                env=environ,
                pass_fds=[listener.fileno()],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        cls.addClassCleanup(stop, cls.process)

    def send(self, raw_request):
        """Send ``raw_request`` as is and parse the response gunicorn writes."""
        try:
            with socket.create_connection(self.address, timeout=30) as conn:
                conn.sendall(raw_request)
                response = http.client.HTTPResponse(conn)
                response.begin()
                response.read()
        except (http.client.HTTPException, OSError) as e:
            log = self.log_path.read_text(encoding="utf-8", errors="replace")
            self.fail(f"no response: {e!r}\ngunicorn log:\n{log}")
        return response

    def assertErrorPage(self, response, status):
        self.assertEqual(response.status, status)
        self.assertEqual(response.getheader("Content-Type"), "text/html")
        self.assertEqual(
            response.msg.get_all("Content-Security-Policy"),
            [build_policy(settings.SECURE_CSP)],
        )

    def test_a_request_line_over_the_limit(self):
        path = b"/" + b"a" * self.cfg.limit_request_line

        response = self.send(b"GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n" % path)

        self.assertErrorPage(response, 400)

    def test_a_header_over_the_limit(self):
        value = b"a" * self.cfg.limit_request_field_size

        response = self.send(
            b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Big: %s\r\n\r\n" % value
        )

        self.assertErrorPage(response, 431)

    def test_a_malformed_request_line(self):
        response = self.send(b"GARBAGE\r\n\r\n")

        self.assertErrorPage(response, 400)

    def test_the_apps_own_response_keeps_its_policy(self):
        response = self.send(
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.msg.get_all("Content-Security-Policy"), [APP_POLICY])


class SyncWorkerErrorPageTest(ErrorPageTests, SimpleTestCase):
    worker_class = "sync"

    def test_the_image_runs_this_worker_class(self):
        self.assertEqual(self.cfg.worker_class_str, self.worker_class)


class ThreadWorkerErrorPageTest(ErrorPageTests, SimpleTestCase):
    # What --threads or --worker-class gthread in gunicorn.extraArgs gives.
    worker_class = "gthread"
