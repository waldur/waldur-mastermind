import logging

from django.test import SimpleTestCase

from waldur_core.logging.sentry import (
    before_send,
    normalize_for_fingerprint,
    parse_structlog_message,
)


def make_record(msg, name="waldur_core.tasks"):
    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


def fingerprint(msg, name="waldur_core.tasks"):
    event = before_send(
        {"logentry": {"message": msg}}, {"log_record": make_record(msg, name)}
    )
    return event["fingerprint"]


class ParseStructlogMessageTest(SimpleTestCase):
    def test_extracts_event_and_context(self):
        parsed = parse_structlog_message(
            "{'event': 'Sent pending order message', 'level': 'info', 'logger': 'marketplace'}"
        )
        self.assertEqual(
            parsed,
            ("Sent pending order message", {"level": "info", "logger": "marketplace"}),
        )

    def test_accepts_dict_directly(self):
        self.assertEqual(
            parse_structlog_message({"event": "hello", "level": "info"}),
            ("hello", {"level": "info"}),
        )

    def test_falls_back_to_error_key(self):
        """Celery task failures put the message under 'error', not 'event'."""
        parsed = parse_structlog_message(
            "{'error': 'The read operation timed out', 'exception': 'Traceback...'}"
        )
        self.assertEqual(parsed[0], "The read operation timed out")
        self.assertEqual(parsed[1], {"exception": "Traceback..."})

    def test_returns_none_for_plain_message(self):
        self.assertIsNone(parse_structlog_message("Unable to get access token"))

    def test_returns_none_for_malformed_input(self):
        self.assertIsNone(parse_structlog_message("{not a dict literal"))
        self.assertIsNone(parse_structlog_message("{'no_message_key': 1}"))
        self.assertIsNone(parse_structlog_message("{1, 2, 3}"))
        self.assertIsNone(parse_structlog_message(None))


class NormalizeForFingerprintTest(SimpleTestCase):
    def test_replaces_hyphenated_uuid(self):
        self.assertEqual(
            normalize_for_fingerprint(
                "task 37d34844-42c3-464b-a652-9e0fcc6b4a2e failed"
            ),
            "task <uuid> failed",
        )

    def test_replaces_embedded_hex_identifier(self):
        self.assertEqual(
            normalize_for_fingerprint(
                "offering_37f4f67fa1ed44118459679ccd50e201_order"
            ),
            "offering_<hex>_order",
        )

    def test_leaves_ordinary_text_alone(self):
        message = "Unable to get access token. Reason: invalid_client"
        self.assertEqual(normalize_for_fingerprint(message), message)


class BeforeSendTest(SimpleTestCase):
    def test_differing_tracebacks_share_one_fingerprint(self):
        """PUHURI-PORTALS-X72/X73/X75/X76 differ only in the rendered traceback."""
        template = (
            "{{'error': '[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error', "
            "'exception': 'Traceback (most recent call last):\\n  File \"x.py\", line {}'}}"
        )
        fingerprints = {
            tuple(fingerprint(template.format(line))) for line in (101, 118, 233)
        }
        self.assertEqual(len(fingerprints), 1)

    def test_identifier_churn_does_not_create_new_groups(self):
        template = "{{'event': 'Pull failed for offering offering_{}_order'}}"
        fingerprints = {
            tuple(fingerprint(template.format(uuid)))
            for uuid in (
                "37f4f67fa1ed44118459679ccd50e201",
                "2a0e7d3ff12743b5843fff837ae51391",
            )
        }
        self.assertEqual(len(fingerprints), 1)

    def test_different_messages_stay_separate(self):
        self.assertNotEqual(
            fingerprint("{'event': 'Broker unreachable'}"),
            fingerprint("{'event': 'Token rejected'}"),
        )

    def test_same_message_from_different_loggers_stays_separate(self):
        self.assertNotEqual(
            fingerprint("{'event': 'boom'}", "waldur_core.a"),
            fingerprint("{'event': 'boom'}", "waldur_core.b"),
        )

    def test_title_is_replaced_and_context_preserved(self):
        msg = (
            "{'event': 'Broker unreachable', 'level': 'error', 'logger': 'marketplace'}"
        )
        event = before_send(
            {"logentry": {"message": msg, "params": []}},
            {"log_record": make_record(msg)},
        )

        self.assertEqual(event["logentry"]["message"], "Broker unreachable")
        self.assertNotIn("params", event["logentry"])
        self.assertEqual(event["extra"]["level"], "error")
        self.assertEqual(event["extra"]["logger"], "marketplace")

    def test_existing_extra_is_not_clobbered(self):
        msg = "{'event': 'Broker unreachable', 'level': 'error'}"
        event = before_send(
            {"logentry": {"message": msg}, "extra": {"level": "kept"}},
            {"log_record": make_record(msg)},
        )
        self.assertEqual(event["extra"]["level"], "kept")

    def test_plain_log_message_keeps_default_grouping(self):
        event = before_send(
            {"logentry": {"message": "plain failure"}},
            {"log_record": make_record("plain failure")},
        )
        self.assertEqual(event["logentry"]["message"], "plain failure")
        self.assertNotIn("fingerprint", event)

    def test_non_log_event_keeps_default_grouping(self):
        """Exception events carry no log_record and must group by stacktrace."""
        event = before_send({"exception": {"values": []}}, {})
        self.assertNotIn("fingerprint", event)
