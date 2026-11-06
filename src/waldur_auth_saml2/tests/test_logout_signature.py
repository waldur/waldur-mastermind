import base64
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from djangosaml2.views import _get_subject_id
from rest_framework.authtoken.models import Token
from saml2 import BINDING_HTTP_REDIRECT
from saml2.attribute_converter import ac_factory
from saml2.mdstore import InMemoryMetaData
from saml2.metadata import entity_descriptor
from saml2.server import Server
from saml2.xmldsig import SIG_RSA_SHA256

from waldur_auth_saml2 import models

from .test_response_signature import (
    KNOWN_IDP,
    UNKNOWN_IDP,
    Saml2SignedMessagesTestCase,
)


class Saml2LogoutRequestSignatureTest(Saml2SignedMessagesTestCase):
    """A logout request from an identity provider ends the browser session
    only if the identity provider the session logged in with signed it."""

    def setUp(self):
        super().setUp()
        self.logout_url = reverse("saml2-logout-complete")
        self.slo_url = settings.SAML_CONFIG["service"]["sp"]["endpoints"][
            "single_logout_service"
        ][0][0]
        response = self.post(self.signed_response(KNOWN_IDP, "known-idp", "alice"))
        self.assertIn("login_completed", response["Location"])
        self.token = Token.objects.get(user=self.user)
        self.subject = _get_subject_id(self.client.session)

    def logout_request(self, entity_id, key_name):
        server = Server(config=self.idp_config(entity_id, key_name))
        return server, server.create_logout_request(
            destination=self.slo_url,
            issuer_entity_id=self.sp_entity_id,
            name_id=self.subject,
            sign=False,
        )

    def redirect(self, entity_id, key_name, sign=True, relay_state="", query=None):
        """The query string the identity provider redirects the browser with."""
        server, (_, request) = self.logout_request(entity_id, key_name)
        http_info = server.apply_binding(
            BINDING_HTTP_REDIRECT,
            str(request),
            self.slo_url,
            relay_state,
            sign=sign,
            sigalg=SIG_RSA_SHA256 if sign else None,
        )
        location = dict(http_info["headers"])["Location"]
        params = {
            key: values[0] for key, values in parse_qs(urlparse(location).query).items()
        }
        params.update(query or {})
        return params

    def post_form(self, entity_id, key_name, sign=True):
        server = Server(config=self.idp_config(entity_id, key_name))
        _, request = server.create_logout_request(
            destination=self.slo_url,
            issuer_entity_id=self.sp_entity_id,
            name_id=self.subject,
            sign=sign,
            sign_alg=SIG_RSA_SHA256 if sign else None,
        )
        return {"SAMLRequest": base64.b64encode(str(request).encode()).decode()}

    def is_logged_in(self):
        return self.client.session.get("_auth_user_id") == str(self.user.pk)

    def assert_session_ended(self, response, tokens_revoked=True):
        self.assertFalse(self.is_logged_in())
        self.assertEqual(
            Token.objects.filter(key=self.token.key).exists(), not tokens_revoked
        )
        # The identity provider gets its answer.
        if response.has_header("Location"):
            location = urlparse(response["Location"])
            self.assertEqual(
                f"{location.scheme}://{location.netloc}{location.path}",
                f"{KNOWN_IDP}/slo",
            )
            self.assertIn("SAMLResponse", parse_qs(location.query))
        else:
            self.assertEqual(response.status_code, 200)
            page = response.content.decode()
            self.assertIn(f'action="{KNOWN_IDP}/slo"', page)
            self.assertIn('name="SAMLResponse"', page)

    def assert_session_kept(self, response):
        self.assertIn("logout_failed", response["Location"])
        self.assertTrue(self.is_logged_in())

    def test_signed_redirect_request_ends_session(self):
        response = self.client.get(
            self.logout_url, self.redirect(KNOWN_IDP, "known-idp")
        )
        self.assert_session_ended(response)

    def test_signed_redirect_request_with_relay_state_ends_session(self):
        response = self.client.get(
            self.logout_url,
            self.redirect(KNOWN_IDP, "known-idp", relay_state="back-to-idp"),
        )
        self.assert_session_ended(response)

    def test_signed_post_request_ends_session(self):
        response = self.client.post(
            self.logout_url, self.post_form(KNOWN_IDP, "known-idp"), format="multipart"
        )
        self.assert_session_ended(response)

    def test_unsigned_redirect_request_keeps_session(self):
        response = self.client.get(
            self.logout_url, self.redirect(KNOWN_IDP, "known-idp", sign=False)
        )
        self.assert_session_kept(response)

    def test_unsigned_post_request_keeps_session(self):
        response = self.client.post(
            self.logout_url,
            self.post_form(KNOWN_IDP, "known-idp", sign=False),
            format="multipart",
        )
        self.assert_session_kept(response)

    def test_redirect_request_signed_with_another_key_keeps_session(self):
        response = self.client.get(
            self.logout_url, self.redirect(KNOWN_IDP, "unknown-idp")
        )
        self.assert_session_kept(response)

    def test_redirect_request_with_altered_relay_state_keeps_session(self):
        response = self.client.get(
            self.logout_url,
            self.redirect(KNOWN_IDP, "known-idp", query={"RelayState": "elsewhere"}),
        )
        self.assert_session_kept(response)

    def test_request_signed_by_another_known_identity_provider_keeps_session(self):
        # A federation lists many identity providers; only the one this
        # session logged in with may log it out.
        metadata = str(entity_descriptor(self.idp_config(UNKNOWN_IDP, "unknown-idp")))
        parsed = InMemoryMetaData(ac_factory(), metadata=metadata)
        parsed.load()
        models.IdentityProvider.objects.create(
            url=UNKNOWN_IDP, name="Another IdP", metadata=parsed[UNKNOWN_IDP]
        )

        response = self.client.get(
            self.logout_url, self.redirect(UNKNOWN_IDP, "unknown-idp")
        )
        self.assert_session_kept(response)

    def test_unsigned_request_ends_session_if_signatures_are_not_required(self):
        with override_settings(
            WALDUR_AUTH_SAML2={
                **settings.WALDUR_AUTH_SAML2,
                "WANT_LOGOUT_REQUESTS_SIGNED": False,
            }
        ):
            response = self.client.get(
                self.logout_url, self.redirect(KNOWN_IDP, "known-idp", sign=False)
            )
        # Anybody could have sent it: the tokens stay.
        self.assert_session_ended(response, tokens_revoked=False)

    def test_unsigned_post_request_ends_session_if_signatures_are_not_required(self):
        with override_settings(
            WALDUR_AUTH_SAML2={
                **settings.WALDUR_AUTH_SAML2,
                "WANT_LOGOUT_REQUESTS_SIGNED": False,
            }
        ):
            response = self.client.post(
                self.logout_url,
                self.post_form(KNOWN_IDP, "known-idp", sign=False),
                format="multipart",
            )
        self.assert_session_ended(response, tokens_revoked=False)

    def test_replayed_request_keeps_the_next_session(self):
        query = self.redirect(KNOWN_IDP, "known-idp")
        self.assert_session_ended(self.client.get(self.logout_url, query))

        response = self.post(self.signed_response(KNOWN_IDP, "known-idp", "alice"))
        self.assertIn("login_completed", response["Location"])
        self.token = Token.objects.get(user=self.user)

        self.assert_session_kept(self.client.get(self.logout_url, query))
        self.assertTrue(Token.objects.filter(key=self.token.key).exists())
