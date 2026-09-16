"""The Celery logging hook must apply Django's config, not a narrower one.

Celery configures logging itself at worker and beat startup, and a receiver on
``setup_logging`` pre-empts its built-in root-logger hijack. The receiver used
to install a reduced configuration of its own, which dropped
``DatabaseLogHandler`` from the root logger — so ``SystemLog`` rows with source
``worker`` or ``beat`` could never be written, and those filters in the admin
log viewer were permanently empty.

Most assertions here inspect the config the hook *passes to* ``dictConfig``
rather than the resulting process state. Applying a config in-process proves
less than it appears: ``disable_existing_loggers: False`` leaves already
configured loggers alone, so levels installed by the test runner's own settings
survive a hook that never mentions them.
"""

import logging
import logging.config
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from waldur_core.server.celeryconf import _configure_structlog_for_celery


def call_hook(loglevel=logging.INFO):
    """Run the hook with dictConfig stubbed; return the config it applied."""
    with mock.patch("logging.config.dictConfig") as dict_config:
        _configure_structlog_for_celery(
            loglevel=loglevel, logfile=None, format=None, colorize=None
        )
    if not dict_config.call_args_list:
        return None
    return dict_config.call_args.args[0]


class CeleryLoggingConfigTest(SimpleTestCase):
    def test_handlers_and_loggers_come_from_settings(self):
        config = call_hook()
        self.assertEqual(config["handlers"], settings.LOGGING["handlers"])
        self.assertEqual(config["loggers"], settings.LOGGING["loggers"])
        self.assertEqual(
            config["root"]["handlers"], settings.LOGGING["root"]["handlers"]
        )

    def test_database_handler_is_among_them(self):
        """Without it, SystemLog source=worker/beat rows are unreachable."""
        config = call_hook()
        classes = [
            config["handlers"][key]["class"] for key in config["root"]["handlers"]
        ]
        self.assertIn("waldur_core.logging.log.DatabaseLogHandler", classes)

    def test_worker_loglevel_overrides_the_root_level(self):
        """A worker started with -l debug still gets a DEBUG root."""
        self.assertEqual(call_hook(loglevel=logging.DEBUG)["root"]["level"], "DEBUG")

    def test_root_level_falls_back_to_settings(self):
        self.assertEqual(
            call_hook(loglevel=None)["root"]["level"],
            settings.LOGGING["root"]["level"],
        )

    def test_settings_logging_is_not_mutated(self):
        before = settings.LOGGING["root"]["level"]
        call_hook(loglevel=logging.DEBUG)
        self.assertEqual(settings.LOGGING["root"]["level"], before)

    @override_settings(DJANGO_STRUCTLOG_CELERY_ENABLED=False)
    def test_hook_is_a_noop_when_disabled(self):
        self.assertIsNone(call_hook())


class CeleryLoggingAppliedTest(SimpleTestCase):
    """One end-to-end check that the config actually lands on the root logger."""

    def setUp(self):
        self.addCleanup(logging.config.dictConfig, settings.LOGGING)

    def test_root_keeps_the_database_handler(self):
        _configure_structlog_for_celery(
            loglevel=logging.INFO, logfile=None, format=None, colorize=None
        )
        self.assertIn(
            "DatabaseLogHandler",
            [type(handler).__name__ for handler in logging.root.handlers],
        )
