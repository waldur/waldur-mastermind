"""Process-wide cache of vCenter sessions.

A vCenter session is server-side state: it lives until it is logged out or until
vCenter expires it, roughly thirty minutes after it last saw traffic. Waldur
builds a backend per Celery task — ``ServiceSettings.get_backend()`` constructs a
fresh instance every time — so a session tied to a backend instance means a login
per pull of every resource, and vCenter caps how many sessions one server will
hold at once.

The cache here matches the way the plugin actually runs: a handful of long-lived
Celery worker processes against a handful of service settings objects. One
connection per service settings per process is kept and handed to every backend
built from those settings, so a sequence of operations logs in once.

Two things follow from the connection outliving the backend that opened it:

* it may have gone stale between uses — vCenter expired it, or was restarted —
  so it is checked before being handed out, at most once per
  ``LIVENESS_CHECK_INTERVAL``;
* nothing collects it, so idle connections are closed here rather than left for
  vCenter to expire: an entry unused for ``IDLE_TIMEOUT`` is logged out on the
  next visit to the cache, and every entry is closed at process exit.

pyVim is imported lazily (see CLAUDE.md, "Lazy imports for heavy optional
dependencies"), which is why the vim25 helpers below import it inside the
function rather than at module level.
"""

import atexit
import hashlib
import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

# How long a connection handed out of the cache is trusted without asking
# vCenter whether the session is still there. The check is one round trip, so
# doing it on every property read of a busy pull would cost more than it saves;
# a minute is short next to vCenter's ~30 minute idle timeout.
LIVENESS_CHECK_INTERVAL = 60

# How long an unused connection is kept before it is logged out. Comfortably
# under vCenter's idle timeout, so that a session ends because Waldur closed it
# rather than because vCenter gave up on it.
IDLE_TIMEOUT = 600


def credentials_fingerprint(*parts):
    """Digest identifying a set of connection parameters.

    Cached entries are keyed by service settings, so an operator editing the
    password or the URL has to invalidate the connection that was opened with
    the old ones. Comparing a digest rather than the values themselves keeps
    credentials out of the cache and out of anything that renders it.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode())
        digest.update(b"\0")
    return digest.hexdigest()


class _CachedConnection:
    __slots__ = ("connection", "fingerprint", "last_used", "last_checked")

    def __init__(self, connection, fingerprint, now):
        self.connection = connection
        self.fingerprint = fingerprint
        self.last_used = now
        self.last_checked = now


class ConnectionCache:
    """Connections to one vCenter endpoint, keyed by service settings.

    :param label: how this endpoint is named in log messages.
    :param is_alive: called with a connection; returns whether the session
        behind it is still usable. Must not raise.
    :param close: called with a connection to log out. Must not raise.
    :param clock: source of monotonic time, so that a test can age an entry
        without freezing the clock for the I/O running alongside it.
    """

    def __init__(self, label, is_alive, close, clock=time.monotonic):
        self.label = label
        self.clock = clock
        self._is_alive = is_alive
        self._close = close
        self._entries = {}
        # One lock for the whole cache, held across the login and the logout as
        # well: connecting under the lock is what stops two callers opening two
        # sessions for the same settings.
        #
        # It also serialises those network calls for *different* settings, and
        # leaves a connection handed to one caller closable by another. Neither
        # can happen in the processes Waldur ships — the Celery pool is prefork
        # and gunicorn runs sync workers (see docker/rootfs/etc/waldur/), so a
        # cache only ever has one thread in it. A deployment that switches
        # either to a threaded or gevent pool would need in-use accounting here.
        self._lock = threading.Lock()

    def acquire(self, key, fingerprint, connect, force_check=False):
        """Return a live connection for ``key``, opening one if needed.

        :param key: identifies the service settings the connection belongs to.
        :param fingerprint: digest of the parameters ``connect`` would use; a
            change means the cached connection was opened with credentials or a
            URL that no longer apply.
        :param connect: called to open a new connection when there is no usable
            one. Its exceptions propagate to the caller.
        :param force_check: ask vCenter about the session even if it was checked
            within ``LIVENESS_CHECK_INTERVAL``. For a caller whose whole purpose
            is to report whether the endpoint answers.
        """
        now = self.clock()
        with self._lock:
            self._close_idle(now)

            entry = self._entries.get(key)
            if entry is not None:
                if entry.fingerprint != fingerprint:
                    logger.info(
                        "Connection parameters for %s changed; reconnecting.",
                        self.label,
                    )
                    self._discard(key, entry)
                elif not self._still_alive(entry, now, force_check):
                    logger.info(
                        "The cached %s session is gone; reconnecting.", self.label
                    )
                    self._discard(key, entry)
                else:
                    entry.last_used = now
                    return entry.connection

            connection = connect()
            self._entries[key] = _CachedConnection(connection, fingerprint, now)
            return connection

    def release(self, key):
        """Log out of the connection cached for ``key``, if there is one."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._discard(key, entry)

    def close_all(self):
        """Log out of every cached connection."""
        with self._lock:
            for key, entry in list(self._entries.items()):
                self._discard(key, entry)

    def _still_alive(self, entry, now, force_check=False):
        """Whether the entry can be handed out without asking vCenter again."""
        if not force_check and now - entry.last_checked < LIVENESS_CHECK_INTERVAL:
            return True
        if not self._is_alive(entry.connection):
            return False
        entry.last_checked = now
        return True

    def _close_idle(self, now):
        for key, entry in list(self._entries.items()):
            if now - entry.last_used >= IDLE_TIMEOUT:
                logger.debug(
                    "Closing an idle %s session for service settings %s.",
                    self.label,
                    key,
                )
                self._discard(key, entry)

    def _discard(self, key, entry):
        del self._entries[key]
        self._close(entry.connection)


def _soap_is_alive(service_instance):
    """Whether vCenter still has the session behind this connection.

    ``currentSession`` is answered from the session manager, so an expired or
    terminated session comes back as None rather than as a fault; a vCenter that
    went away in the meantime surfaces as a transport error instead.
    """
    try:
        return service_instance.content.sessionManager.currentSession is not None
    except Exception:
        logger.debug("The cached vim25 session did not answer.", exc_info=True)
        return False


def _soap_close(service_instance):
    """Log out of vCenter, ignoring a failure to do so.

    Also called from an atexit hook, where a session vCenter has already expired
    is not worth reporting. ``pyVim.connect`` is taken off ``sys.modules`` rather
    than imported: an import this late can find the machinery behind it already
    torn down, and nothing could have opened this connection without the module
    being loaded already.
    """
    try:
        connect = sys.modules.get("pyVim.connect")
        if connect is None:
            return
        connect.Disconnect(service_instance)
    except Exception:
        logger.debug("Failed to close the vim25 session.", exc_info=True)


def _rest_is_alive(client):
    # The cache requires this not to raise, and VMwareClient does not promise
    # that: _request wraps the transport, but a 200 carrying something other
    # than JSON (a proxy's error page, say) surfaces as a JSONDecodeError from
    # outside its try. Unwrapped, that would reach a pull task, which catches
    # only ServiceBackendError.
    try:
        return client.is_session_alive()
    except Exception:
        logger.debug("The cached REST session did not answer.", exc_info=True)
        return False


def _rest_close(client):
    try:
        client.close()
    except Exception:
        logger.debug("Failed to close the REST session.", exc_info=True)


soap_sessions = ConnectionCache("vim25", _soap_is_alive, _soap_close)
rest_sessions = ConnectionCache("REST", _rest_is_alive, _rest_close)


def close_all_sessions(**_kwargs):
    """Log out of every vCenter session this process holds.

    Registered both for interpreter exit and, from the app config, for Celery
    worker process shutdown: a worker that stops between pulls would otherwise
    leave its sessions for vCenter to expire. Accepts the keyword arguments
    Celery passes its signal handlers.
    """
    soap_sessions.close_all()
    rest_sessions.close_all()


atexit.register(close_all_sessions)
