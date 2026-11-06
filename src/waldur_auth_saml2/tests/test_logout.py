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
from saml2 import BINDING_HTTP_REDIRECT
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
IDP = "https://idp.example.com/idp/shibboleth"


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
        self.saml_config = get_config.start().return_value
        self.addCleanup(get_config.stop)

        client_class = mock.patch("saml2.client.Saml2Client")
        self.saml_client = client_class.start().return_value
        self.addCleanup(client_class.stop)
        self.saml_client.handle_logout_request.return_value = {
            "headers": [("Location", IDP_LOGOUT_URL)]
        }
        # The session logged in with IDP, which sends the logout requests.
        self.saml_client.users.issuers_of_info.return_value = [IDP]
        self.saml_client.parse_logout_request.return_value.sender.return_value = IDP

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

    def assert_session_ended(self, tokens_revoked):
        self.assertEqual(
            Token.objects.filter(user=self.user).exists(), not tokens_revoked
        )
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
        self.assert_session_ended(tokens_revoked=True)

    def test_logout_requested_by_another_identity_provider_keeps_session(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "subject-of-the-user"
        )
        self.saml_client.parse_logout_request.return_value.sender.return_value = (
            "https://other-idp.example.org/idp/shibboleth"
        )

        self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assert_still_logged_in()

    def test_redirect_binding_signature_is_passed_to_pysaml2(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "subject-of-the-user"
        )

        self.client.get(
            self.url,
            {
                "SAMLRequest": LOGOUT_REQUEST,
                "RelayState": "relay",
                "SigAlg": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
                "Signature": "c2lnbmF0dXJl",
            },
        )

        signed_with = {
            "relay_state": "relay",
            "sigalg": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "signature": "c2lnbmF0dXJl",
        }
        self.saml_client.parse_logout_request.assert_called_once_with(
            LOGOUT_REQUEST, BINDING_HTTP_REDIRECT, **signed_with
        )
        self.saml_client.handle_logout_request.assert_called_once_with(
            LOGOUT_REQUEST, mock.ANY, BINDING_HTTP_REDIRECT, **signed_with
        )

    def test_signed_logout_requests_are_required_by_default(self):
        self.start_saml2_session()

        self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.saml_config.setattr.assert_called_once_with(
            "idp", "want_authn_requests_signed", True
        )

    def test_signed_logout_requests_can_be_made_optional(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "subject-of-the-user"
        )

        with override_settings(
            WALDUR_AUTH_SAML2={
                **settings.WALDUR_AUTH_SAML2,
                "WANT_LOGOUT_REQUESTS_SIGNED": False,
            }
        ):
            self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.saml_config.setattr.assert_called_once_with(
            "idp", "want_authn_requests_signed", False
        )
        # Anybody could have sent an unsigned request: the tokens stay.
        self.assert_session_ended(tokens_revoked=False)

    def test_logout_request_is_accepted_once(self):
        self.start_saml2_session()
        self.saml_client.parse_logout_request.return_value.message.name_id = name_id(
            "subject-of-the-user"
        )
        self.saml_client.parse_logout_request.return_value.message.id = "request-1"
        self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})
        self.assertFalse(self.is_authenticated())

        # The user logs in again; the same request is sent once more.
        self.client.force_login(self.user)
        Token.objects.get_or_create(user=self.user)
        self.start_saml2_session()
        response = self.client.get(self.url, {"SAMLRequest": LOGOUT_REQUEST})

        self.assertIn("logout_failed", response["Location"])
        self.assert_still_logged_in()

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
        # The portal's logout revokes the token before it starts this one.
        self.assert_session_ended(tokens_revoked=False)

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
        self.assert_session_ended(tokens_revoked=True)

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


class Saml2LogoutStartTest(test.APITestCase):
    """/api-auth/saml2/logout/ sends the browser to the identity provider."""

    def setUp(self):
        cache.clear()
        self.url = reverse("saml2-logout")
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
        self.saml_client.global_logout.return_value = {
            IDP: (BINDING_HTTP_REDIRECT, {"headers": [("Location", IDP_LOGOUT_URL)]})
        }

        self.user = structure_factories.UserFactory()
        self.client.force_login(self.user)
        session = self.client.session
        _set_subject_id(session, name_id("subject-of-the-user"))
        session[AUTHENTICATION_METHOD_KEY] = AuthenticationMethod.SAML2
        session.save()

    def test_logout_is_not_started_for_a_browser_still_logged_in(self):
        # A link on another site must not log the user out of the IdP.
        response = self.client.get(self.url)

        self.assertIn("logout_failed", response["Location"])
        self.saml_client.global_logout.assert_not_called()

    def test_logout_is_started_after_the_portal_logout(self):
        token, _ = Token.objects.get_or_create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        with override_settings(
            WALDUR_AUTH_SAML2={
                **settings.WALDUR_AUTH_SAML2,
                "ENABLE_SINGLE_LOGOUT": True,
            }
        ):
            response = self.client.post(reverse("auth-logout"))
        self.assertEqual(response.data["logout_url"], "http://testserver" + self.url)
        self.client.credentials()

        response = self.client.get(self.url)

        self.assertEqual(response["Location"], IDP_LOGOUT_URL)
        self.saml_client.global_logout.assert_called_once()
