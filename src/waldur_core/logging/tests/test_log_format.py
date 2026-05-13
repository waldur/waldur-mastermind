"""Regression tests for log formatting of stdlib loggers via structlog."""

import io
import json
import logging
import unittest

import structlog

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
