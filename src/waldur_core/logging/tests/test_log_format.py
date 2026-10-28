"""Regression tests for log formatting of stdlib loggers via structlog."""

import io
import json
import logging
import unittest

import structlog
from django.core.servers.basehttp import WSGIRequestHandler
from django.test import RequestFactory, SimpleTestCase

from waldur_core.logging.log import EventLoggerAdapter, RequireEvent, RequireNotEvent
from waldur_core.server.base_settings import _FOREIGN_PRE_CHAIN


class ForeignPreChainExcInfoTest(unittest.TestCase):
    """Verify that stdlib loggers (e.g. django.request) emit a formatted
    multi-line traceback string instead of repr(traceback_object) when
    ``exc_info=True`` is passed.
    """

    def test_exc_info_is_formatted_as_traceback_string(self):
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        )
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(formatter)
        test_logger = logging.getLogger("waldur_test_log_format")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.ERROR)
        test_logger.propagate = False
        try:
            try:
                raise ValueError("boom")
            except ValueError:
                test_logger.error("Internal Server Error", exc_info=True)

            record = json.loads(buf.getvalue())
            # structlog's format_exc_info renames exc_info to "exception"
            self.assertIn("exception", record)
            self.assertIsInstance(record["exception"], str)
            self.assertIn("ValueError: boom", record["exception"])
            self.assertIn("Traceback", record["exception"])
        finally:
            test_logger.removeHandler(handler)


class EventLoggerAdapterTest(unittest.TestCase):
    """structlog renders the message under "event", so the marker that tells
    event records apart must not reuse that key."""

    def setUp(self):
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        self.logger = logging.getLogger("waldur_test_event_format")
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.addCleanup(self.logger.removeHandler, handler)

    def test_event_message_survives_the_json_formatter(self):
        EventLoggerAdapter(self.logger).info(
            "EVENT_LOG: Resource has been created.",
            extra={"event_type": "resource_creation_succeeded", "event_context": {}},
        )
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        )

        rendered = json.loads(formatter.format(self.records[0]))

        self.assertEqual(rendered["event"], "EVENT_LOG: Resource has been created.")
        self.assertEqual(rendered["event_type"], "resource_creation_succeeded")

    def test_filters_tell_event_records_apart(self):
        EventLoggerAdapter(self.logger).info("EVENT_LOG: An event.")
        self.logger.info("Not an event.")
        event_record, plain_record = self.records

        self.assertTrue(RequireEvent().filter(event_record))
        self.assertFalse(RequireNotEvent().filter(event_record))
        self.assertFalse(RequireEvent().filter(plain_record))
        self.assertTrue(RequireNotEvent().filter(plain_record))


class ForeignPreChainRequestTest(SimpleTestCase):
    """Django passes the request to django.request and django.security, and
    the renderers write it as its repr, query string included."""

    def _render(self, request):
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        )
        record = logging.LogRecord(
            "django.request", logging.WARNING, __file__, 1, "Forbidden", (), None
        )
        record.request = request
        return json.loads(formatter.format(record))["request"]

    def test_a_credential_in_the_query_is_masked(self):
        request = RequestFactory().get("/x/?nick=a&access_token=secret&page=2")

        self.assertEqual(
            self._render(request),
            "<WSGIRequest: GET '/x/?nick=a&access_token=***&page=2'>",
        )

    def test_any_other_request_renders_as_before(self):
        request = RequestFactory().get("/api/customers/?page=2&o=name")

        self.assertEqual(self._render(request), repr(request))


class RunserverRequestLineTest(SimpleTestCase):
    """runserver logs the request line of every request, query included, and
    a homeserver in the matrix-dev stack calls it with its hs_token."""

    def _log(self, request_line):
        server = WSGIRequestHandler.__new__(WSGIRequestHandler)
        server.request = None
        stream = io.StringIO()
        console = logging.getHandlerByName("console")
        previous = console.setStream(stream)
        try:
            server.log_message('"%s" %s %s', request_line, "200", "2")
        finally:
            console.setStream(previous)
        return stream.getvalue()

    def test_a_credential_in_the_query_is_masked(self):
        log = self._log("PUT /_matrix/app/v1/ping?access_token=secret HTTP/1.1")

        self.assertNotIn("secret", log)
        self.assertIn("/_matrix/app/v1/ping?access_token=*** HTTP/1.1", log)

    def test_any_other_request_line_is_kept(self):
        log = self._log("GET /api/customers/?page=2&o=name HTTP/1.1")

        self.assertIn("GET /api/customers/?page=2&o=name HTTP/1.1", log)
