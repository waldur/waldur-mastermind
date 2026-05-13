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
from django.test import Client, RequestFactory
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from waldur_core.core.authentication import SessionAuthentication
from waldur_core.structure.tests import factories as structure_factories


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


class AdminLoginUnaffectedByCsrfChangeTest(APITestCase):
    """
    /admin login goes through Django's `CsrfViewMiddleware`, not DRF's
    `SessionAuthentication`. Removing the DRF override (SEC-C1) must not
    break the admin login flow: a normal CSRF-tokened POST must still
    log a staff user in.
    """

    def test_csrf_tokened_admin_login_succeeds(self):
        password = "correct-horse-battery-staple"  # noqa: S105
        staff = structure_factories.UserFactory(is_staff=True, is_active=True)
        staff.set_password(password)
        staff.save()

        client = Client(enforce_csrf_checks=True)

        # GET the login page first so Django sets a csrftoken cookie.
        get_response = client.get("/admin/login/")
        self.assertEqual(get_response.status_code, 200)
        csrf_token = client.cookies["csrftoken"].value

        # POST credentials with the matching token. Django's
        # CsrfViewMiddleware accepts either the form field or the header.
        response = client.post(
            "/admin/login/",
            data={
                "username": staff.username,
                "password": password,
                "csrfmiddlewaretoken": csrf_token,
            },
        )

        # 302 means the login form was accepted by CsrfViewMiddleware
        # AND credentials matched. (The exact redirect target depends on
        # LOGIN_REDIRECT_URL and any `next` param; we don't care here —
        # we only care that admin's CSRF path still works.)
        self.assertEqual(
            response.status_code,
            302,
            f"Expected admin login to succeed (302 redirect); got "
            f"{response.status_code}: {response.content[:300]!r}. "
            f"If this fails, the SEC-C1 change has affected Django "
            f"admin's CsrfViewMiddleware path — investigate.",
        )

    def test_admin_login_post_without_csrf_token_is_rejected(self):
        # Confirms the standard Django CSRF protection on admin still
        # fires — independent of any DRF changes.
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/admin/login/",
            data={"username": "anyone", "password": "anything"},
        )
        self.assertEqual(
            response.status_code,
            403,
            f"Django admin should reject CSRF-less POST; got {response.status_code}.",
        )
