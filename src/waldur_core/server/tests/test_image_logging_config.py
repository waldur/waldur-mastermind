"""Guard logging configuration against being replaced by an image config file.

``/etc/waldur/*.conf.py`` are ``exec``'d by the image settings module *after*
``base_settings`` (``docker/rootfs/etc/waldur/settings.py``), so a file that
*assigns* ``LOGGING`` replaces it wholesale and silently discards every logger
level configured in ``base_settings`` — including the ``neutronclient``
suppression. That happened, and nothing caught it: docker-compose bind-mounts
its own empty ``logging.conf.py`` over the image's, so the one deployment with
automated coverage was the one deployment where the bug could not appear.
"""

import copy
import logging
import pathlib

from django.test import SimpleTestCase

from waldur_core.server.base_settings import LOGGING as BASE_LOGGING

IMAGE_CONFIG_DIR = (
    pathlib.Path(__file__).parents[4] / "docker" / "rootfs" / "etc" / "waldur"
)


def apply_conf_file(path):
    """Return LOGGING as the image sees it: base_settings, then ``path``."""
    namespace = {"LOGGING": copy.deepcopy(BASE_LOGGING)}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace["LOGGING"]


def effective_level(config, name):
    """Resolve the level a logger ends up with under ``config``.

    Deliberately computed from the config dict rather than from
    ``logging.getLogger(name).getEffectiveLevel()``: Django calls
    ``dictConfig`` once per process, and a second call with
    ``disable_existing_loggers: False`` leaves already-configured loggers
    alone. An in-process check would therefore read the levels the test
    runner's own settings installed and pass against a broken config.
    """
    loggers = config["loggers"]
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        entry = loggers.get(".".join(parts[:i]))
        if entry and "level" in entry:
            return logging.getLevelNamesMapping()[entry["level"]]
    return logging.getLevelNamesMapping()[config["root"]["level"]]


class ShippedConfigFilesTest(SimpleTestCase):
    """Whatever the image ships must add to LOGGING, never replace it."""

    def test_no_shipped_conf_file_drops_a_base_logger(self):
        conf_files = sorted(IMAGE_CONFIG_DIR.glob("*.conf.py"))
        self.assertTrue(conf_files, f"no *.conf.py found in {IMAGE_CONFIG_DIR}")
        for path in conf_files:
            for name, config in BASE_LOGGING["loggers"].items():
                with self.subTest(file=path.name, logger=name):
                    loggers = apply_conf_file(path)["loggers"]
                    self.assertIn(name, loggers)
                    self.assertEqual(loggers[name], config)


class BaseLoggingCoversEveryDeploymentTest(SimpleTestCase):
    """Deltas that used to live in the image config must stay in base_settings.

    Anything moved back out of here reaches helm but not docker-compose, which
    is how the two drifted apart in the first place.
    """

    def test_neutronclient_deprecation_notice_is_suppressed(self):
        """One line per neutron client, ~7 per tenant on every pull cycle."""
        self.assertGreater(
            effective_level(BASE_LOGGING, "neutronclient.v2_0.client"),
            logging.WARNING,
        )

    def test_celery_boot_chatter_is_quiet(self):
        for name in ("celery.bootsteps", "celery.loaders", "celery.utils.imports"):
            with self.subTest(logger=name):
                self.assertEqual(effective_level(BASE_LOGGING, name), logging.WARNING)

    def test_console_handler_writes_to_stdout(self):
        self.assertEqual(
            BASE_LOGGING["handlers"]["console"]["stream"], "ext://sys.stdout"
        )

    def test_celery_loggers_inherit_root_handlers(self):
        """Level-only entries, so a handler added to root still reaches them."""
        for name, entry in BASE_LOGGING["loggers"].items():
            if name.startswith("celery"):
                with self.subTest(logger=name):
                    self.assertNotIn("handlers", entry)
                    self.assertNotIn("propagate", entry)
