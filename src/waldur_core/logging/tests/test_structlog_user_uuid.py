import ast
import copy
import json
import logging
from contextlib import contextmanager
from io import StringIO

import structlog
from django.conf import settings
from django.test import override_settings
from rest_framework import test

from waldur_core.structure.tests import factories, fixtures

# Override to enable request logs in this test (production uses WARNING to suppress them)
_TEST_LOGGING = copy.deepcopy(settings.LOGGING)
_TEST_LOGGING.setdefault("loggers", {})["django_structlog"] = {"level": "INFO"}


def _parse_log_record(record):
    """Extract dict from record - supports 'LEVEL:logger:{...}' or raw JSON."""
    try:
        # Try raw JSON first
        return json.loads(record)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        # Format: LEVEL:logger_name:{dict repr}
        parts = record.split(":", 2)
        if len(parts) >= 3:
            return ast.literal_eval(parts[2])
    except (ValueError, SyntaxError):
        pass
    return None


@contextmanager
def _capture_structlog_output(logger_name):
    """Context manager that captures logs with structlog formatter. Yields list of log lines (like assertLogs cm.output)."""
    root = logging.getLogger()
    buf = StringIO()
    formatter = None
    if root.handlers:
        formatter = root.handlers[0].formatter
    if formatter is None:
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.ExtraAdder(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
            ],
        )
    capture = logging.StreamHandler(buf)
    capture.setFormatter(formatter)
    logger = logging.getLogger(logger_name)
    logger.addHandler(capture)
    output = []
    try:
        yield output
    finally:
        logger.removeHandler(capture)
        output[:] = [
            line.strip() for line in buf.getvalue().split("\n") if line.strip()
        ]


class StructlogUserUuidTest(test.APITestCase):
    """Verify that structlog binds user_uuid (not user_id) for authenticated requests."""

    @override_settings(LOGGING=_TEST_LOGGING)
    def test_authenticated_request_logs_contain_user_uuid(self):
        user = factories.UserFactory()
        self.client.force_authenticate(user)

        with self.assertLogs("django_structlog", level="INFO") as cm:
            self.client.get(factories.CustomerFactory.get_list_url())

        # user_uuid appears in request_finished (not request_started) for DRF auth
        found_user_uuid = False
        expected_hex = user.uuid.hex
        for record in cm.output:
            data = _parse_log_record(record)
            if data and "user_uuid" in data:
                # Log may use hyphenated or hex format
                actual = str(data["user_uuid"]).replace("-", "")
                if actual == expected_hex:
                    found_user_uuid = True
                    break

        self.assertTrue(
            found_user_uuid,
            msg=f"Expected user_uuid in logs for authenticated request. Output: {cm.output}",
        )

    def test_manual_log_includes_structlog_context(self):
        """Verify manual logger.info() during HTTP request includes request_id and user_uuid."""
        fixture = fixtures.UserFixture()
        notification = factories.NotificationFactory(
            key="test.structlog_manual_log",
            enabled=False,
        )
        enable_url = factories.NotificationFactory.get_url(
            notification, action="enable"
        )

        with _capture_structlog_output("waldur_core.structure.views") as cm:
            self.client.force_authenticate(fixture.staff)
            response = self.client.post(enable_url)

        self.assertEqual(response.status_code, 200)

        found_context = False
        expected_hex = fixture.staff.uuid.hex

        for line in cm:
            data = _parse_log_record(line)
            if not data:
                continue
            # Must be from our view (notification enable)
            if data.get("logger") != "waldur_core.structure.views":
                continue
            has_user_uuid = (
                "user_uuid" in data
                and str(data.get("user_uuid", "")).replace("-", "") == expected_hex
            )
            has_request_id = (
                "request_id" in data and len(str(data.get("request_id", ""))) >= 32
            )
            if has_user_uuid and has_request_id:
                found_context = True
                break

        self.assertTrue(
            found_context,
            msg=f"Expected user_uuid and request_id in manual log. Output: {cm}",
        )
