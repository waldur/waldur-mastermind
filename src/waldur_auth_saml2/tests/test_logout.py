import base64
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from djangosaml2.cache import StateCache
from djangosaml2.views import _set_subject_id
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from saml2.saml import NAMEID_FORMAT_TRANSIENT, NameID

from waldur_core.core.authentication import (
    AUTHENTICATION_METHOD_KEY,
    AuthenticationMethod,
)
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories

LOGOUT_REQUEST = base64.b64encode(b"<samlp:LogoutRequest/>").decode()
LOGOUT_RESPONSE = base64.b64encode(b"<samlp:LogoutResponse/>").decode()
IDP_LOGOUT_URL = "https://idp.example.com/slo"


def name_id(text):
    return NameID(format=NAMEID_FORMAT_TRANSIENT, text=text)


class Saml2LogoutCompleteTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("saml2-logout-complete")
        settings_override = override_settings(
            WALDUR_CORE={**settings.WALDUR_CORE, "AUTHENTICATION_METHODS": ["SAML2"]}
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        get_config = mock.patch("djangosaml2.conf.get_config")
        get_config.start()
        self.addCleanup(get_config.stop)

        client_class = mock.patch("saml2.client.Saml2Client")
        self.saml_client = client_class.start().return_value
        self.addCleanup(client_class.stop)
        self.saml_client.handle_logout_request.return_value = {
            "headers": [("Location", IDP_LOGOUT_URL)]
        }

        # A SAML2 login leaves the user with a Django session and an API token.
        self.user = structure_factories.UserFactory()
        self.client.force_login(self.user)
        Token.objects.get_or_create(user=self.user)

    def start_saml2_session(
        self, subject="subject-of-the-user", logout_request_id=None
    ):
        session = self.client.session
        _set_subject_id(session, name_id(subject))
        if logout_request_id:
            state = StateCache(session)
            state[logout_request_id] = {"operation": "SLO"}
            state.sync()
        session.save()

    def is_authenticated(self):
        return (
            self.client.get("http://testserver/api/").status_code == status.HTTP_200_OK
        )

    def assert_logged_out(self):
        self.assertFalse(Token.objects.filter(user=self.user).exists())
        self.assertFalse(self.is_authenticated())
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.AUTH_LOGGED_OUT_WITH_SAML2
            ).exists()
        )

    def assert_still_logged_in(self):
        self.assertTrue(Token.objects.filter(user=self.user).exists())
        self.assertTrue(self.is_authenticated())

    def test_logout_requested_by_identity_provider_for_this_subject_ends_session(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "subject-of-the-user"
        )

        response = self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response["Location"], IDP_LOGOUT_URL)
        self.assert_logged_out()

    def test_logout_requested_for_another_subject_keeps_session(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "somebody-else"
        )

        self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assert_still_logged_in()

    def test_logout_request_without_saml2_login_keeps_session(self):
        # A link to this endpoint must not log a password or admin session out.
        response = self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.saml_client.handle_logout_request.assert_not_called()
        self.assert_still_logged_in()

    def test_logout_response_to_our_request_ends_session(self):
        self.start_saml2_session(logout_request_id="logout-request-id")
        self.saml_client.parse_logout_request_response.return_value.in_response_to = (
            "logout-request-id"
        )

        response = self.client.get(self.url, {"SAMLResponse": LOGOUT_RESPONSE})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assert_logged_out()

    def test_logout_response_to_another_request_keeps_session(self):
        self.start_saml2_session(logout_request_id="logout-request-id")
        self.saml_client.parse_logout_request_response.return_value.in_response_to = (
            "unknown-request-id"
        )

        self.client.get(self.url, {"SAMLResponse": LOGOUT_RESPONSE})

        self.assert_still_logged_in()

    def test_malformed_logout_message_keeps_session(self):
        self.start_saml2_session(logout_request_id="logout-request-id")
        self.saml_client.parse_logout_request_response.side_effect = ValueError(
            "bad signature"
        )

        response = self.client.get(self.url, {"SAMLResponse": LOGOUT_RESPONSE})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("logout_failed", response["Location"])
        self.assert_still_logged_in()

    def test_confirmed_logout_request_ends_session_even_if_the_reply_fails(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "subject-of-the-user"
        )
        self.saml_client.handle_logout_request.side_effect = ValueError(
            "no usable response binding"
        )

        response = self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assertIn("logout_failed", response["Location"])
        self.assert_logged_out()

    def test_logout_without_session_succeeds(self):
        self.client.logout()

        response = self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(Token.objects.filter(user=self.user).exists())

    def test_single_logout_from_the_portal_leaves_no_saml2_state(self):
        # The portal logs out, the browser follows the SAML2 logout to the IdP
        # and comes back with its answer.
        self.start_saml2_session()
        session = self.client.session
        session[AUTHENTICATION_METHOD_KEY] = AuthenticationMethod.SAML2
        session.save()
        token = Token.objects.get(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        with override_settings(
            WALDUR_AUTH_SAML2={
                **settings.WALDUR_AUTH_SAML2,
                "ENABLE_SINGLE_LOGOUT": True,
            }
        ):
            response = self.client.post(reverse("auth-logout"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.credentials()
        self.assertFalse(self.is_authenticated())

        # What /api-auth/saml2/logout/ records when it sends the request.
        session = self.client.session
        state = StateCache(session)
        state["logout-request-id"] = {"operation": "SLO"}
        state.sync()
        session.save()
        self.saml_client.parse_logout_request_response.return_value.in_response_to = (
            "logout-request-id"
        )

        response = self.client.get(self.url, {"SAMLResponse": LOGOUT_RESPONSE})

        self.assertIn("logout_completed", response["Location"])
        self.assertFalse(
            [key for key in self.client.session.keys() if key.startswith("_saml2")]
        )
