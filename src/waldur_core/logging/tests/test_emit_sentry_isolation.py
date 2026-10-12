import logging

import sentry_sdk
from django.test import TestCase
from sentry_sdk._types import AnnotatedValue
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from waldur_core.logging import sentry
from waldur_core.logging.enums import EventType
from waldur_core.logging.event_logger import emit, event_logger
from waldur_core.logging.models import Event


class _Discard(Transport):
    def capture_envelope(self, envelope):
        pass


class _CaptureLogContext(logging.Handler):
    """Record the context as handlers see it, before Sentry scrubs in place."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.event_context = None
        self.attributes_at_log_time = None

    def emit(self, record):
        context = getattr(record, "event_context", None)
        if context is None:
            return
        self.event_context = context
        attributes = context["resource_attributes"]
        # Copy the values now. Sentry replaces them on this same dict after
        # the handlers return.
        self.attributes_at_log_time = {
            "flavor": attributes["flavor"],
            "token": attributes["token"],
            "session": attributes["session"],
        }


class EmitSentryIsolationTest(TestCase):
    def setUp(self):
        self.previous_client = sentry_sdk.get_client()
        self.capture = _CaptureLogContext()
        event_logger.logger.addHandler(self.capture)
        sentry_sdk.init(
            dsn="https://key@sentry.invalid/1",
            default_integrations=False,
            integrations=[
                LoggingIntegration(
                    level=None,
                    event_level=logging.ERROR,
                    sentry_logs_level=None,
                )
            ],
            transport=_Discard(),
            event_scrubber=sentry.event_scrubber(),
        )

    def tearDown(self):
        event_logger.logger.removeHandler(self.capture)
        sentry_sdk.get_global_scope().set_client(self.previous_client)

    def test_error_event_stays_json_safe_when_sentry_scrubs_the_log_copy(self):
        """Sentry redacts denylisted keys in place on the log record.

        Resource attributes often carry those keys (token, session). The audit
        row must keep the original values, and the line handed to log handlers
        must already have credential-named keys masked: handlers render before
        Sentry's own scrubber runs.
        """
        attributes = {
            "flavor": "large",
            "token": "backend-token",
            # On Sentry's denylist, and not a key Waldur's scrubber masks, so
            # only the in-place scrub can corrupt it.
            "session": "session-value",
        }
        # Marketplace failure events are dropped when NOTIFY_ABOUT_RESOURCE_CHANGE
        # is off. This test is about emit() itself.
        emit(
            "Resource {resource_name} creation has failed.",
            event_type=EventType.PROJECT_UPDATE_SUCCEEDED,
            event_context={
                "resource_name": "my-vm",
                "resource_attributes": attributes,
            },
            level="error",
        )

        event = Event.objects.get(event_type=EventType.PROJECT_UPDATE_SUCCEEDED)
        event.refresh_from_db()
        stored = event.context["resource_attributes"]
        self.assertEqual(event.message, "Resource my-vm creation has failed.")
        self.assertEqual(stored["flavor"], "large")
        self.assertEqual(stored["token"], "backend-token")
        self.assertEqual(stored["session"], "session-value")
        self.assertEqual(attributes["token"], "backend-token")

        logged = self.capture.attributes_at_log_time
        self.assertEqual(logged["flavor"], "large")
        self.assertEqual(logged["token"], "***")
        self.assertEqual(logged["session"], "session-value")

        scrubbed = self.capture.event_context["resource_attributes"]
        self.assertIsInstance(scrubbed["session"], AnnotatedValue)
        self.assertIsInstance(scrubbed["token"], AnnotatedValue)
        self.assertEqual(stored["session"], "session-value")
        self.assertEqual(stored["token"], "backend-token")
