"""
Regression test for the fix to Critical security finding #7.

The JIRA, Zammad, and SMAX webhook receivers previously had
`authentication_classes = ()` and `permission_classes = ()` with no
HMAC, signature, or shared secret — any internet host could POST and
trigger backend resyncs and `Order.output` writes.

The fix adds an opt-in per-integration shared-secret check via the
`X-Webhook-Secret` header, with the expected value stored in Constance
(`JIRA_WEBHOOK_SHARED_SECRET`, `ZAMMAD_WEBHOOK_SHARED_SECRET`,
`SMAX_WEBHOOK_SHARED_SECRET`). Default is empty → authentication is not
enforced (backward compatible). Setting any of the secrets switches
that receiver into authenticated mode.
"""

from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase


class _BaseWebhookAuthCases:
    """Shared test cases. Subclasses set `url_name` and `secret_setting`."""

    url_name = ""  # subclass override
    secret_setting = ""  # subclass override
    test_secret = "x" * 40

    @property
    def url(self):
        return reverse(self.url_name)

    def test_empty_secret_setting_skips_auth(self):
        # No Constance override → secret is "" → check is skipped and the
        # request reaches the view body. Anything other than 401/403/503
        # proves we did not gate the request behind shared-secret auth.
        response = self.client.post(self.url, data={}, format="json")
        self.assertNotIn(
            response.status_code,
            (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ),
            f"Receiver {self.url_name} rejected an unauthenticated request "
            f"when {self.secret_setting} is unset — should be backward "
            f"compatible (got {response.status_code}).",
        )

    def test_missing_header_is_rejected(self):
        with override_constance_config(**{self.secret_setting: self.test_secret}):
            response = self.client.post(self.url, data={}, format="json")
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Receiver {self.url_name} accepted a request with no header.",
        )

    def test_wrong_header_value_is_rejected(self):
        with override_constance_config(**{self.secret_setting: self.test_secret}):
            response = self.client.post(
                self.url,
                data={},
                format="json",
                HTTP_X_WEBHOOK_SECRET="wrong",
            )
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Receiver {self.url_name} accepted a request with wrong "
            f"X-Webhook-Secret header.",
        )

    def test_correct_header_passes_auth(self):
        # With auth passing, request reaches the serializer and fails on
        # validation (empty body). Anything other than 401/403/503 proves
        # we got past the auth gate.
        with override_constance_config(**{self.secret_setting: self.test_secret}):
            response = self.client.post(
                self.url,
                data={},
                format="json",
                HTTP_X_WEBHOOK_SECRET=self.test_secret,
            )
        self.assertNotIn(
            response.status_code,
            (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ),
            f"Receiver {self.url_name} did not let a correctly-signed "
            f"request past auth (got {response.status_code}).",
        )


class JiraWebhookAuthTest(_BaseWebhookAuthCases, APITestCase):
    url_name = "web-hook-receiver"
    secret_setting = "JIRA_WEBHOOK_SHARED_SECRET"


class ZammadWebhookAuthTest(_BaseWebhookAuthCases, APITestCase):
    url_name = "zammad-web-hook-receiver"
    secret_setting = "ZAMMAD_WEBHOOK_SHARED_SECRET"


class SmaxWebhookAuthTest(_BaseWebhookAuthCases, APITestCase):
    url_name = "smax-web-hook-receiver"
    secret_setting = "SMAX_WEBHOOK_SHARED_SECRET"
