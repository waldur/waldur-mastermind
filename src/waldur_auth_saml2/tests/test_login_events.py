import base64
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from rest_framework import status, test
from saml2.response import StatusRequestDenied

from waldur_core.core.enums import LoginFailureCode
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event


class Saml2CompleteTestCase(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("saml2-login-complete")
        waldur_core = {
            **settings.WALDUR_CORE,
            "AUTHENTICATION_METHODS": ["SAML2"],
        }
        settings_override = override_settings(WALDUR_CORE=waldur_core)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        get_config = mock.patch("djangosaml2.conf.get_config")
        get_config.start()
        self.addCleanup(get_config.stop)

        client_class = mock.patch("saml2.client.Saml2Client")
        self.saml_client = client_class.start().return_value
        self.addCleanup(client_class.stop)

    def post_response(self):
        return self.client.post(
            self.url, {"SAMLResponse": base64.b64encode(b"<Response/>").decode()}
        )


class Saml2LoginFailedEventTest(Saml2CompleteTestCase):
    def test_response_denied_by_identity_provider_is_logged(self):
        self.saml_client.parse_authn_request_response.side_effect = StatusRequestDenied(
            "denied"
        )

        response = self.post_response()

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        event = Event.objects.get(event_type=EventType.AUTH_LOGIN_FAILED_WITH_SAML2)
        self.assertIn("denied by identity provider", event.context["reason"])

    def test_response_without_assertion_is_logged(self):
        self.saml_client.parse_authn_request_response.return_value.assertion = None

        self.post_response()

        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.AUTH_LOGIN_FAILED_WITH_SAML2
            ).exists()
        )

    def test_repeated_failure_from_one_client_is_logged_once(self):
        self.saml_client.parse_authn_request_response.return_value.assertion = None

        self.post_response()
        self.post_response()

        self.assertEqual(
            Event.objects.filter(
                event_type=EventType.AUTH_LOGIN_FAILED_WITH_SAML2
            ).count(),
            1,
        )


class Saml2LoginFailedCodeTest(Saml2CompleteTestCase):
    """Each failure sends the browser on with its code, and with no text."""

    def assert_code(self, response, code):
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        location = urlparse(response["Location"])
        self.assertEqual(location.path, "/login_failed/")
        self.assertEqual(parse_qs(location.query), {"code": [code]})

    def test_denied(self):
        self.saml_client.parse_authn_request_response.side_effect = StatusRequestDenied(
            "denied"
        )
        self.assert_code(self.post_response(), LoginFailureCode.SAML2_DENIED)

    def test_unparseable_response(self):
        self.saml_client.parse_authn_request_response.side_effect = ValueError(
            "<script>bad</script>"
        )
        self.assert_code(self.post_response(), LoginFailureCode.SAML2_INVALID_RESPONSE)

    def test_empty_response(self):
        self.saml_client.parse_authn_request_response.return_value = None
        self.assert_code(self.post_response(), LoginFailureCode.SAML2_EMPTY_RESPONSE)

    def test_response_without_assertion(self):
        self.saml_client.parse_authn_request_response.return_value.assertion = None
        self.assert_code(self.post_response(), LoginFailureCode.SAML2_NO_ASSERTION)

    @mock.patch("waldur_auth_saml2.views.auth.authenticate")
    def test_validation_error_keeps_its_text_out_of_the_url(self, authenticate):
        self.saml_client.parse_authn_request_response.return_value.assertion = object()
        text = "Visit https://attacker.example to fix your account"
        authenticate.side_effect = ValidationError(text)

        response = self.post_response()

        self.assert_code(response, LoginFailureCode.SAML2_REJECTED)
        self.assertNotIn("attacker", response["Location"])
        event = Event.objects.get(event_type=EventType.AUTH_LOGIN_FAILED_WITH_SAML2)
        self.assertEqual(event.context["reason"], text)

    @mock.patch("waldur_auth_saml2.views.auth.authenticate")
    def test_list_validation_error_is_rejected_not_500(self, authenticate):
        self.saml_client.parse_authn_request_response.return_value.assertion = object()
        authenticate.side_effect = ValidationError(
            ["Email is required.", "Name is too long."]
        )

        response = self.post_response()

        self.assert_code(response, LoginFailureCode.SAML2_REJECTED)
        event = Event.objects.get(event_type=EventType.AUTH_LOGIN_FAILED_WITH_SAML2)
        self.assertEqual(
            event.context["reason"], "Email is required.; Name is too long."
        )

    @mock.patch("waldur_auth_saml2.views.auth.authenticate", return_value=None)
    def test_no_user(self, authenticate):
        self.saml_client.parse_authn_request_response.return_value.assertion = object()
        self.assert_code(self.post_response(), LoginFailureCode.SAML2_FAILED)
