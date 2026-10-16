import base64
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework import status, test
from saml2.response import StatusRequestDenied

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event


class Saml2LoginFailedEventTest(test.APITestCase):
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
