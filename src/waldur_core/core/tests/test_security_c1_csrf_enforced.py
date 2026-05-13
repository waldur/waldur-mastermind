"""
Regression test for the fix to Critical security finding #1.

`SessionAuthentication.enforce_csrf` previously returned `None`, silently
skipping CSRF on every cookie-session-authenticated state-changing API
call. With CORS_ORIGIN_ALLOW_ALL enabled, any browser holding a Waldur
session cookie was a target for cross-site forged writes.

The Waldur SPA never uses session cookies (it sends Authorization: Token
or Bearer), so removing the override does not affect normal frontend
traffic. This test verifies that DRF's default enforcement now triggers.
"""

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from waldur_core.core.authentication import SessionAuthentication


class SessionAuthCsrfEnforcedTest(APITestCase):
    def test_unsafe_request_without_csrf_token_is_rejected(self):
        factory = RequestFactory()
        # State-changing method without an `X-CSRFToken` header / cookie.
        request = factory.post(
            "/api/anything/", data={}, content_type="application/json"
        )
        # Attach an empty session so the auth machinery has somewhere to look.
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()

        auth = SessionAuthentication()
        with self.assertRaises(PermissionDenied) as ctx:
            auth.enforce_csrf(request)
        self.assertIn(
            "CSRF",
            str(ctx.exception),
            "DRF's default enforce_csrf should raise PermissionDenied "
            "with a CSRF-related message; the override may still be "
            "swallowing the check.",
        )

    def test_override_method_no_longer_defined_on_subclass(self):
        # If somebody re-introduces the override, the bypass is back.
        self.assertNotIn(
            "enforce_csrf",
            SessionAuthentication.__dict__,
            "SessionAuthentication has re-defined enforce_csrf; the CSRF "
            "bypass has returned.",
        )
