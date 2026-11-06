"""Tripwires: side effects a dry run must not reach fail it instead.

While a thread's run is armed, sending mail, queueing or running a Celery
task, publishing to the broker, registering an ``on_commit`` callback,
writing to the cache, opening a network connection (over ``requests``,
``urllib3``, ``http.client``, ``httpx`` or a bare socket, to anything but the
database and the cache), writing a file to storage, creating a marketplace
order and starting a thread raise ``DryRunSideEffect``. System roles are
looked up without the process-wide cache: one a run creates is gone with it.

Arming is per thread and context: a request served meanwhile by another
thread calls straight through. A thread started inside a run would start
with a fresh, unarmed context, so starting one is refused instead.

Kept apart from ``dry_run.py`` so that the modules the dry run itself
imports -- the call templates, through the serializers -- can arm it too.
"""

import contextlib
import functools
import http.client
import socket
import threading
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
import kombu
import requests
import urllib3
from celery import Celery
from celery.app.task import Task as CeleryTask
from celery.canvas import Signature
from django.conf import settings
from django.core import mail as django_mail
from django.core.cache import caches
from django.core.files.storage import FileSystemStorage, Storage
from django.core.mail.message import EmailMessage
from django.db import transaction as transaction_module
from django.db.backends.base.base import BaseDatabaseWrapper

from waldur_core.core import middleware as core_middleware
from waldur_core.core import utils as core_utils
from waldur_core.permissions.models import Role
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import dry_run_context


class DryRunSideEffect(RuntimeError):
    """A side effect was reached inside a dry run. The run fails, and
    everything it did is rolled back."""


_armed = ContextVar("proposal_dry_run_armed", default=False)
_install_lock = threading.Lock()
# The database and cache addresses are resolved once, as the tripwires are
# first installed.
_installed = {"count": 0, "saved": [], "addresses": None}

# Writes that could carry what a dry run made out of its transaction. Taking
# an entry out cannot -- the value is computed again on the next read -- and
# reading a Constance setting nobody has stored yet does just that, so
# deletion is left alone; emptying the whole cache is not.
CACHE_WRITES = (
    "set",
    "add",
    "set_many",
    "incr",
    "decr",
    "touch",
    "clear",
    "get_or_set",
)


def is_armed():
    return _armed.get()


def _tripwire_targets():
    """``(owner, attribute, label)`` of every side effect a dry run must not
    reach. Class attributes, so every subclass and every way in is covered:
    ``send_mail`` and ``broadcast_mail`` end in ``EmailMessage.send``,
    ``.delay`` in ``apply_async``, ``transaction.on_commit`` in the
    connection's ``on_commit``. Module attributes bound under another name
    elsewhere -- ``core_utils.get_connection`` -- are wrapped there too."""
    return [
        (EmailMessage, "send", "mail"),
        (django_mail, "send_mass_mail", "mail"),
        (django_mail, "get_connection", "mail"),
        (core_utils, "get_connection", "mail"),
        (core_utils, "send_mail", "mail"),
        (core_utils, "broadcast_mail", "mail"),
        (CeleryTask, "apply_async", "celery"),
        (CeleryTask, "apply", "celery"),
        (Signature, "apply_async", "celery"),
        (Celery, "send_task", "celery"),
        (kombu.Producer, "publish", "celery"),
        (BaseDatabaseWrapper, "on_commit", "on_commit"),
        (transaction_module, "on_commit", "on_commit"),
        (requests.sessions.Session, "send", "http"),
        (urllib3.connectionpool.HTTPConnectionPool, "urlopen", "http"),
        (http.client.HTTPConnection, "connect", "http"),
        (httpx.Client, "send", "http"),
        (httpx.AsyncClient, "send", "http"),
        (Storage, "save", "storage"),
        (FileSystemStorage, "delete", "storage"),
        (marketplace_models.Order, "save", "marketplace_order"),
        (threading.Thread, "start", "thread"),
    ]


def _guard(original, label):
    @functools.wraps(original)
    def guarded(*args, **kwargs):
        if _armed.get():
            raise DryRunSideEffect(f"A dry run reached a side effect: {label}.")
        return original(*args, **kwargs)

    guarded.__dry_run_guard__ = True
    return guarded


def _service_addresses():
    """The ``(address, port)`` pairs of the database and the cache servers: a
    run reads from both, and may need a connection to do so. Resolved once,
    unarmed, as the tripwires are installed."""
    endpoints = []
    for database in settings.DATABASES.values():
        host = database.get("HOST")
        if host and not host.startswith("/"):
            endpoints.append((host, int(database.get("PORT") or 5432)))
    for cache in settings.CACHES.values():
        locations = cache.get("LOCATION") or []
        if isinstance(locations, str):
            locations = locations.split(";") if "://" in locations else [locations]
        for location in locations:
            parts = urlsplit(location if "://" in location else f"//{location}")
            if parts.hostname:
                endpoints.append((parts.hostname, parts.port or 6379))
    addresses = set()
    for host, port in endpoints:
        try:
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                addresses.add((info[4][0], info[4][1]))
        except (OSError, UnicodeError):
            continue
    return frozenset(addresses)


def _connection_allowed(sock, address):
    if sock.family != socket.AF_INET and sock.family != socket.AF_INET6:
        # A local socket: the database or the cache on this host.
        return True
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    return (address[0], address[1]) in (_installed["addresses"] or ())


def _socket_guard(original):
    @functools.wraps(original)
    def guarded(sock, address, *args, **kwargs):
        if _armed.get() and not _connection_allowed(sock, address):
            raise DryRunSideEffect("A dry run reached a side effect: network.")
        return original(sock, address, *args, **kwargs)

    guarded.__dry_run_guard__ = True
    return guarded


def _install():
    """Wrap every target, once for all concurrent runs. A wrapper raises only
    in a thread whose run is armed and calls through everywhere else, so a
    request served meanwhile is untouched."""
    with _install_lock:
        if _installed["count"] == 0:
            saved = []
            for owner, name, label in _tripwire_targets():
                original = owner.__dict__.get(name, getattr(owner, name))
                setattr(owner, name, _guard(getattr(owner, name), label))
                saved.append((owner, name, original))
            if _installed["addresses"] is None:
                _installed["addresses"] = _service_addresses()
            for name in ("connect", "connect_ex"):
                original = socket.socket.__dict__.get(
                    name, getattr(socket.socket, name)
                )
                setattr(
                    socket.socket, name, _socket_guard(getattr(socket.socket, name))
                )
                saved.append((socket.socket, name, original))
            _installed["saved"] = saved
        _installed["count"] += 1


def _uninstall():
    with _install_lock:
        _installed["count"] -= 1
        if _installed["count"] == 0:
            for owner, name, original in reversed(_installed["saved"]):
                setattr(owner, name, original)
            _installed["saved"] = []


@contextlib.contextmanager
def tripwires():
    """Arm the tripwires for the current thread.

    The cache backends are per thread, so their write methods are wrapped on
    this thread's instances alone. Inside an armed run it is a no-op: the
    run's own tripwires stay as they are, and are taken down by whoever put
    them up.
    """
    if _armed.get():
        yield
        return
    _install()
    backends = [caches[alias] for alias in settings.CACHES]
    for backend in backends:
        for name in CACHE_WRITES:
            if hasattr(backend, name):
                setattr(backend, name, _guard(getattr(backend, name), "cache"))
    token = _armed.set(True)
    try:
        with Role.objects.uncached():
            yield
    finally:
        _armed.reset(token)
        for backend in backends:
            for name in CACHE_WRITES:
                if getattr(backend.__dict__.get(name), "__dry_run_guard__", False):
                    delattr(backend, name)
        _uninstall()


@contextlib.contextmanager
def disarmed():
    """Call through the tripwires for a moment: for what the run reads from
    tables it never writes, or reports about itself."""
    token = _armed.set(False)
    try:
        yield
    finally:
        _armed.reset(token)


class NullCollector:
    """Where the engine's mail and allocation go while a call is set up for
    a check: nowhere. Nothing is sent, nothing is rendered."""

    def step_event(self, instance, trigger):
        pass

    def change_request(self, change_request, event):
        pass

    def changes_submitted(self, closed):
        pass

    def allocation(self, proposal):
        pass

    def mails(self, mails, trigger, instance=None):
        pass


@contextlib.contextmanager
def sandboxed():
    """Everything set up for a check -- a throwaway organisation, a call
    imported into it, its people -- with the signal handlers quiet, the
    engine's mail collected and dropped, and the tripwires armed."""
    with (
        core_middleware.skip_side_effects(),
        dry_run_context.collecting(NullCollector()),
        tripwires(),
    ):
        yield
