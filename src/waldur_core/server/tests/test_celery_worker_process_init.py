"""A new pool child must survive inheriting a database connection that is lost.

A prefork pool child starts with copies of the worker main process's database
connections, and Celery's Django fixup closes their sockets in the child. When
the server had already dropped the main process's connection, psycopg 3 refuses
``fileno()`` with ``OperationalError``, which the fixup does not expect: every
child forked after that logged "Signal handler ... on_worker_process_init ...
raised: OperationalError('the connection is lost')" and skipped the rest of its
cleanup.
"""

from celery import signals
from celery.fixups.django import DjangoWorkerFixup
from django.db import DEFAULT_DB_ALIAS, OperationalError, connections
from django.test import SimpleTestCase

from waldur_core.core.tests.helpers import lost_default_connection
from waldur_core.server.celeryconf import app, forget_lost_database_connections


class LostInheritedConnectionTest(SimpleTestCase):
    databases = {DEFAULT_DB_ALIAS}

    def test_the_fixup_alone_fails_on_it(self):
        """The production failure, which the receiver exists to prevent."""
        with lost_default_connection():
            try:
                DjangoWorkerFixup(app).on_worker_process_init()
            except OperationalError as exc:
                self.assertIn("the connection is lost", str(exc))
            else:
                self.skipTest(
                    "Celery's fixup now tolerates a lost connection, so "
                    "forget_lost_database_connections can be removed."
                )

    def test_the_child_forgets_it_before_the_fixup_runs(self):
        with lost_default_connection():
            forget_lost_database_connections()

            self.assertIsNone(connections[DEFAULT_DB_ALIAS].connection)
            DjangoWorkerFixup(app).on_worker_process_init()

    def test_a_live_connection_is_left_to_the_fixup(self):
        wrapper = connections[DEFAULT_DB_ALIAS]
        wrapper.ensure_connection()
        live = wrapper.connection

        forget_lost_database_connections()

        self.assertIs(wrapper.connection, live)

    def test_a_new_child_starts_cleanly_with_the_fixup_connected(self):
        """Both receivers connected in the worker's order, through the real signal.

        The worker connects the fixup's receiver at start, after this module was
        imported. Here the test connects it, so the order holds by construction:
        what this checks is that in that order a child starts without an error.
        """
        fixup_receiver = DjangoWorkerFixup(app).on_worker_process_init
        signals.worker_process_init.connect(fixup_receiver, weak=False)
        self.addCleanup(signals.worker_process_init.disconnect, fixup_receiver)

        with lost_default_connection():
            responses = signals.worker_process_init.send(sender=None)

        receivers = [receiver for receiver, _ in responses]
        self.assertLess(
            receivers.index(forget_lost_database_connections),
            receivers.index(fixup_receiver),
        )
        self.assertEqual(
            [error for _, error in responses if isinstance(error, Exception)], []
        )
