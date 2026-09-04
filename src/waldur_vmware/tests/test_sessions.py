"""The connection cache itself, without a vCenter behind it.

The behaviour that matters here — reuse, invalidation, liveness, idle cleanup —
is bookkeeping over callbacks, so it is asserted against stub connections rather
than against the simulator: these run in every shard, while the vcsim tests are
opt-in (see conftest).
"""

import contextlib
from unittest import TestCase, mock

from waldur_vmware import sessions


class ConnectionCacheTest(TestCase):
    def setUp(self):
        self.connections = []
        self.closed = []
        self.alive = True
        self.now = 0.0
        self.cache = sessions.ConnectionCache(
            "test",
            is_alive=lambda connection: self.alive,
            close=self.closed.append,
            clock=lambda: self.now,
        )

    def connect(self):
        """Stand in for a login, returning a connection distinct from the last."""
        connection = f"connection-{len(self.connections)}"
        self.connections.append(connection)
        return connection

    def acquire(self, key=1, fingerprint="fingerprint"):
        return self.cache.acquire(key, fingerprint, self.connect)

    @contextlib.contextmanager
    def clock_moved_on(self, seconds):
        """Run the block as if the cache had been left alone for `seconds`."""
        self.now += seconds
        try:
            yield
        finally:
            self.now -= seconds

    def test_a_second_caller_gets_the_connection_the_first_one_opened(self):
        self.assertEqual(self.acquire(), self.acquire())
        self.assertEqual(len(self.connections), 1)

    def test_different_settings_get_connections_of_their_own(self):
        self.assertNotEqual(self.acquire(key=1), self.acquire(key=2))

    def test_changed_parameters_replace_the_connection(self):
        stale = self.acquire(fingerprint="before")

        fresh = self.acquire(fingerprint="after")

        self.assertNotEqual(stale, fresh)
        self.assertEqual(self.closed, [stale])

    def test_a_connection_is_not_rechecked_within_the_liveness_interval(self):
        self.acquire()
        self.alive = False

        # A dead connection is still handed out here: the point is that nothing
        # asked, which is what keeps a busy pull from paying a round trip per
        # property read.
        with self.clock_moved_on(sessions.LIVENESS_CHECK_INTERVAL - 1):
            self.acquire()

        self.assertEqual(len(self.connections), 1)

    def test_force_check_asks_within_the_liveness_interval(self):
        """What ping() needs: an answer about now, not about a minute ago."""
        stale = self.acquire()
        self.alive = False

        fresh = self.cache.acquire(1, "fingerprint", self.connect, force_check=True)

        self.assertNotEqual(stale, fresh)
        self.assertEqual(self.closed, [stale])

    def test_a_dead_connection_is_replaced_once_the_interval_passes(self):
        stale = self.acquire()
        self.alive = False

        with self.clock_moved_on(sessions.LIVENESS_CHECK_INTERVAL + 1):
            fresh = self.acquire()

        self.assertNotEqual(stale, fresh)
        self.assertEqual(self.closed, [stale])

    def test_a_live_connection_survives_the_liveness_check(self):
        with self.clock_moved_on(sessions.LIVENESS_CHECK_INTERVAL + 1):
            self.acquire()

        self.assertEqual(len(self.connections), 1)
        self.assertEqual(self.closed, [])

    def test_an_idle_connection_is_closed_rather_than_left_to_expire(self):
        idle = self.acquire(key=1)

        with self.clock_moved_on(sessions.IDLE_TIMEOUT + 1):
            self.acquire(key=2)

        self.assertEqual(self.closed, [idle])

    def test_a_connection_in_use_is_not_treated_as_idle(self):
        with self.clock_moved_on(sessions.IDLE_TIMEOUT - 1):
            self.acquire()

        self.assertEqual(self.closed, [])

    def test_releasing_closes_the_connection_for_those_settings_only(self):
        first = self.acquire(key=1)
        self.acquire(key=2)

        self.cache.release(key=1)

        self.assertEqual(self.closed, [first])

    def test_releasing_settings_that_never_connected_is_harmless(self):
        self.cache.release(key=1)

        self.assertEqual(self.closed, [])

    def test_a_released_connection_is_reopened_on_the_next_call(self):
        stale = self.acquire()
        self.cache.release(key=1)

        self.assertNotEqual(stale, self.acquire())

    def test_close_all_closes_every_connection_once(self):
        first = self.acquire(key=1)
        second = self.acquire(key=2)

        self.cache.close_all()
        self.cache.close_all()

        self.assertCountEqual(self.closed, [first, second])

    def test_a_failed_connection_attempt_is_not_cached(self):
        with mock.patch.object(self, "connect", side_effect=RuntimeError("no route")):
            with self.assertRaises(RuntimeError):
                self.acquire()

        self.assertEqual(self.acquire(), "connection-0")


class RestLivenessTest(TestCase):
    """`_rest_is_alive` guards the cache's "is_alive must not raise" contract.

    VMwareClient does not promise it: `_request` wraps the transport, but a 200
    carrying something other than JSON raises from outside that try. Unwrapped,
    it would reach a pull task, which catches only ServiceBackendError.
    """

    def test_an_expired_session_is_reported_dead(self):
        client = mock.Mock()
        client.is_session_alive.return_value = False

        self.assertFalse(sessions._rest_is_alive(client))

    def test_an_unexpected_exception_is_reported_dead(self):
        client = mock.Mock()
        client.is_session_alive.side_effect = ValueError("not JSON")

        self.assertFalse(sessions._rest_is_alive(client))


class CredentialsFingerprintTest(TestCase):
    def test_same_parameters_give_the_same_digest(self):
        self.assertEqual(
            sessions.credentials_fingerprint("vcenter", "user", "secret", False),
            sessions.credentials_fingerprint("vcenter", "user", "secret", False),
        )

    def test_an_edited_password_changes_the_digest(self):
        self.assertNotEqual(
            sessions.credentials_fingerprint("vcenter", "user", "secret", False),
            sessions.credentials_fingerprint("vcenter", "user", "rotated", False),
        )

    def test_the_digest_does_not_carry_the_credentials(self):
        digest = sessions.credentials_fingerprint("vcenter", "user", "secret", False)

        self.assertNotIn("secret", digest)

    def test_parameters_are_not_run_together(self):
        """A digest over concatenated values would collide on a shifted boundary."""
        self.assertNotEqual(
            sessions.credentials_fingerprint("vcenter", "user", "secret"),
            sessions.credentials_fingerprint("vcenter", "users", "ecret"),
        )
