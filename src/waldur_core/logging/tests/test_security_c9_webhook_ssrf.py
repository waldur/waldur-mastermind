"""
Regression test for the fix to Critical security finding #9.

`WebHook.destination_url` accepted any URL — link-local AWS metadata,
loopback, RFC1918 — and `WebHook.process()` issued the request with no
timeout and follow-redirects enabled. That was a free SSRF primitive for
any user who could create a webhook.

The fix adds a `validate_outbound_url` validator on the field and
re-validates at request time inside `process()`, plus pins a timeout
and disables redirects.
"""

from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_core.core.utils import validate_outbound_url
from waldur_core.logging import models as logging_models


class ValidateOutboundUrlTest(TestCase):
    def test_rejects_loopback_and_private_addresses(self):
        ssrf_targets = [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8000/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://[::1]:6379/",
        ]
        for url in ssrf_targets:
            with self.assertRaises(ValidationError, msg=f"{url!r} should be rejected"):
                validate_outbound_url(url)

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "gopher://localhost/", "ftp://x/y"):
            with self.assertRaises(ValidationError):
                validate_outbound_url(url)

    def test_accepts_public_addresses(self):
        # 8.8.8.8 is Google DNS — globally routable and stable. Validator
        # must not raise.
        validate_outbound_url("https://8.8.8.8/anything")


class WebHookFieldValidationTest(TestCase):
    def test_model_full_clean_rejects_internal_url(self):
        non_url_fields = [
            f.name
            for f in logging_models.WebHook._meta.fields
            if f.name != "destination_url"
        ]
        hook = logging_models.WebHook(
            destination_url="http://169.254.169.254/latest/meta-data/",
            content_type=logging_models.WebHook.ContentTypeChoices.JSON,
        )
        with self.assertRaises(ValidationError) as ctx:
            hook.clean_fields(exclude=non_url_fields)
        self.assertIn("destination_url", ctx.exception.error_dict)


class WebHookProcessTest(TestCase):
    def test_process_skips_request_for_internal_url(self):
        # Construct a hook in-memory whose URL re-validates as unsafe,
        # bypassing the model validator (e.g. an old row already in DB).
        hook = logging_models.WebHook(
            destination_url="http://169.254.169.254/latest/meta-data/",
            content_type=logging_models.WebHook.ContentTypeChoices.JSON,
        )
        with mock.patch("waldur_core.logging.models.requests.post") as mock_post:
            event = mock.Mock()
            event.created.isoformat.return_value = "2026-01-01T00:00:00"
            event.message = "x"
            event.context = {}
            event.event_type = "test"
            hook.process(event)
        self.assertFalse(
            mock_post.called,
            "requests.post was called for an internal URL — the request-time "
            "re-validation no longer protects against DNS rebinding.",
        )

    def test_process_passes_timeout_and_disables_redirects_for_safe_url(self):
        hook = logging_models.WebHook(
            destination_url="https://8.8.8.8/anything",
            content_type=logging_models.WebHook.ContentTypeChoices.JSON,
        )
        with mock.patch("waldur_core.logging.models.requests.post") as mock_post:
            event = mock.Mock()
            event.created.isoformat.return_value = "2026-01-01T00:00:00"
            event.message = "x"
            event.context = {}
            event.event_type = "test"
            hook.process(event)
        self.assertTrue(mock_post.called, "Public URL should have been called.")
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs.get("allow_redirects"), False)
        self.assertIsNotNone(
            kwargs.get("timeout"),
            "Webhook outbound request must have a timeout set.",
        )
