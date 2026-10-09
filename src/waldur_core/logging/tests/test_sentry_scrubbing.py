import json
import logging

import sentry_sdk
from django.test import SimpleTestCase
from sentry_sdk.transport import Transport

from waldur_core.logging import sentry


class _Capture(Transport):
    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event:
            self.events.append(event)


def _fail_with_tokens():
    headers = {"Authorization": "Bearer header-token"}  # noqa: F841
    path = "/_matrix/client/v3/sync?access_token=path-token&timeout=0"  # noqa: F841
    auth_header = "Bearer syt_d2FsZHVy_RmTq3cPxYwKzLnVb8sHd_2vLqZx"  # noqa: F841
    as_token = "appservice-token"  # noqa: F841
    hs_token = "homeserver-token"  # noqa: F841
    access_token = "user-access-token"  # noqa: F841
    refresh_token = "user-refresh-token"  # noqa: F841
    registration_secret = "registration-secret"  # noqa: F841
    client_secret = "sso-client-secret"  # noqa: F841
    room_id = "!room:example.org"  # noqa: F841
    raise RuntimeError("homeserver unreachable")


def _fail_with_encryption_secrets():
    # Separate from _fail_with_tokens: Sentry keeps only the first few locals
    # of a frame, so one long list would hide the names this test is about.
    recovery_key = "EsSz ykH7 LCZx 7Cae"  # noqa: F841
    pickle_key = "store-pickle-key"  # noqa: F841
    session_key = "exported-megolm-session"  # noqa: F841
    temporary_password = "reset-password"  # noqa: F841
    lease = "crypto-lease"  # noqa: F841
    room_id = "!room:example.org"  # noqa: F841
    raise RuntimeError("escrow failed")


def _send(action):
    """Run action against a real client and return the events it sends."""
    transport = _Capture()
    client = sentry_sdk.Client(
        dsn="https://key@sentry.invalid/1",
        transport=transport,
        event_scrubber=sentry.event_scrubber(),
        before_send=sentry.before_send,
        before_breadcrumb=sentry.before_breadcrumb,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        action(scope)
    client.flush()
    return transport.events


def _capture_failure(scope):
    try:
        _fail_with_tokens()
    except RuntimeError:
        scope.capture_exception()


class EventScrubberTest(SimpleTestCase):
    def _frame_vars(self):
        events = _send(_capture_failure)
        frames = events[0]["exception"]["values"][0]["stacktrace"]["frames"]
        return next(f["vars"] for f in frames if f["function"] == "_fail_with_tokens")

    def test_matrix_and_sso_secrets_in_frame_locals_are_filtered(self):
        # Sentry's default denylist matches exact names such as "token", so
        # the Matrix tokens and secrets Waldur keeps in locals got through.
        frame_vars = self._frame_vars()

        for name in (
            "as_token",
            "hs_token",
            "access_token",
            "refresh_token",
            "registration_secret",
            "client_secret",
        ):
            self.assertEqual(frame_vars[name], "[Filtered]", name)
        self.assertEqual(frame_vars["room_id"], "'!room:example.org'")

    def test_encryption_secrets_in_frame_locals_are_filtered(self):
        def capture(scope):
            try:
                _fail_with_encryption_secrets()
            except RuntimeError:
                scope.capture_exception()

        frames = _send(capture)[0]["exception"]["values"][0]["stacktrace"]["frames"]
        frame_vars = next(
            f["vars"]
            for f in frames
            if f["function"] == "_fail_with_encryption_secrets"
        )
        for name in (
            "recovery_key",
            "pickle_key",
            "session_key",
            "temporary_password",
            "lease",
        ):
            self.assertEqual(frame_vars[name], "[Filtered]", name)
        self.assertEqual(frame_vars["room_id"], "'!room:example.org'")

    def test_secrets_inside_values_and_nested_dicts_are_filtered(self):
        # Names alone miss them: nio puts the appservice token into every
        # request path, and httpx keeps the Authorization header in a dict.
        frame_vars = self._frame_vars()

        self.assertNotIn("header-token", str(frame_vars["headers"]))
        self.assertNotIn("path-token", frame_vars["path"])
        self.assertIn("/_matrix/client/v3/sync", frame_vars["path"])
        self.assertNotIn("RmTq3cPxYwKzLnVb8sHd", frame_vars["auth_header"])

    def test_tokens_in_the_request_query_string_are_filtered(self):
        event = {
            "request": {
                "url": "https://waldur.example.com/_matrix/app/v1/transactions/1",
                "query_string": "access_token=query-token&x=1",
            }
        }

        redacted = sentry.redact_secrets(event, {})

        self.assertNotIn("query-token", str(redacted))
        self.assertIn("x=1", redacted["request"]["query_string"])

    def test_secrets_in_structlog_context_are_filtered(self):
        # structlog hands logging its event dict as the message. before_send
        # moves its keys into extra after the scrubber has run, and sentry-sdk
        # puts the dict's repr in logentry, which the scrubber never reads.
        events = _send(
            lambda scope: logging.getLogger(__name__).error(
                {
                    "event": "Homeserver call failed",
                    "hs_token": "homeserver-token",
                    "room_id": "!room:example.org",
                }
            )
        )

        self.assertNotIn("homeserver-token", json.dumps(events))
        self.assertEqual(events[0]["extra"]["hs_token"], "[Filtered]")
        self.assertEqual(events[0]["extra"]["room_id"], "!room:example.org")

    def test_secrets_in_structlog_breadcrumbs_are_filtered(self):
        # Lines below the event level become breadcrumbs of the next event,
        # with the dict's repr as their message.
        def log_then_fail(scope):
            logging.getLogger(__name__).info(
                {
                    "event": "Calling homeserver",
                    "as_token": "appservice-token",
                    "room_id": "!room:example.org",
                }
            )
            scope.capture_message("Homeserver call failed")

        events = _send(log_then_fail)

        self.assertNotIn("appservice-token", json.dumps(events))
        crumb = events[0]["breadcrumbs"]["values"][-1]
        self.assertEqual(crumb["message"], "Calling homeserver")
        self.assertEqual(crumb["data"]["room_id"], "!room:example.org")

    def test_basic_credentials_are_filtered(self):
        message = "Authorization: Basic d2FsZHVyOnMzY3JldA=="

        redacted = sentry.redact_secrets({"logentry": {"message": message}}, {})

        self.assertNotIn("d2FsZHVyOnMzY3JldA", str(redacted))

    def test_words_after_basic_and_bearer_are_kept(self):
        # Both scheme names also start ordinary text, and Sentry sends the
        # source lines around every frame.
        message = "Using the basic support backend. Bearer token expired."

        redacted = sentry.redact_secrets({"logentry": {"message": message}}, {})

        self.assertEqual(redacted["logentry"]["message"], message)
