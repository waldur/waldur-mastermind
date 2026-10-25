import base64
import copy
import datetime
import os
import re
import shutil
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from djangosaml2.conf import get_config
from rest_framework import status, test
from saml2 import BINDING_HTTP_REDIRECT
from saml2.attribute_converter import ac_factory
from saml2.config import IdPConfig
from saml2.mdstore import InMemoryMetaData
from saml2.metadata import entity_descriptor
from saml2.s_utils import decode_base64_and_inflate
from saml2.saml import NAME_FORMAT_URI, NAMEID_FORMAT_TRANSIENT, NameID
from saml2.server import Server

from waldur_auth_saml2 import models
from waldur_core.structure.tests import factories as structure_factories

XMLSEC_BINARY = shutil.which("xmlsec1")
KNOWN_IDP = "https://idp.example.com/idp/shibboleth"
UNKNOWN_IDP = "https://unknown-idp.example.org/idp/shibboleth"


def write_keypair(directory, name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_file = os.path.join(directory, f"{name}.key")
    cert_file = os.path.join(directory, f"{name}.crt")
    with open(key_file, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return key_file, cert_file


@unittest.skipUnless(XMLSEC_BINARY, "xmlsec1 is required to sign and verify SAML")
class Saml2ResponseSignatureTest(test.APITestCase):
    """Only identity providers known from metadata may authenticate users.

    The responses here are really signed, so pysaml2 verifies them with xmlsec1
    exactly as it does in a deployment.
    """

    def setUp(self):
        cache.clear()
        self.url = reverse("saml2-login-complete")
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.keys = {
            name: write_keypair(directory, name)
            for name in ("service-provider", "known-idp", "unknown-idp")
        }

        sp_key, sp_cert = self.keys["service-provider"]
        saml_config = copy.deepcopy(settings.SAML_CONFIG)
        saml_config.pop("attribute_map_dir", None)
        saml_config.update(
            xmlsec_binary=XMLSEC_BINARY,
            key_file=sp_key,
            cert_file=sp_cert,
            encryption_keypairs=[{"key_file": sp_key, "cert_file": sp_cert}],
        )
        # Deployments accept IdP-initiated responses, see the helm saml2.conf.py.
        saml_config["service"]["sp"]["allow_unsolicited"] = True

        overrides = override_settings(
            SAML_CONFIG=saml_config,
            SAML_ATTRIBUTE_MAPPING={"uid": ("username",)},
            WALDUR_CORE={**settings.WALDUR_CORE, "AUTHENTICATION_METHODS": ["SAML2"]},
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.sp_entity_id = saml_config["entityid"]
        self.acs_url = saml_config["service"]["sp"]["endpoints"][
            "assertion_consumer_service"
        ][0][0]
        self.sp_metadata = str(entity_descriptor(get_config()))

        known_idp_metadata = str(
            entity_descriptor(self.idp_config(KNOWN_IDP, "known-idp"))
        )
        parsed = InMemoryMetaData(ac_factory(), metadata=known_idp_metadata)
        parsed.load()
        models.IdentityProvider.objects.create(
            url=KNOWN_IDP, name="Known IdP", metadata=parsed[KNOWN_IDP]
        )

        self.user = structure_factories.UserFactory(username="alice")

    def idp_config(self, entity_id, key_name):
        key_file, cert_file = self.keys[key_name]
        config = IdPConfig()
        config.load(
            {
                "entityid": entity_id,
                "xmlsec_binary": XMLSEC_BINARY,
                "service": {
                    "idp": {
                        "endpoints": {
                            "single_sign_on_service": [
                                (f"{entity_id}/sso", BINDING_HTTP_REDIRECT)
                            ]
                        },
                        "policy": {
                            "default": {
                                "lifetime": {"minutes": 15},
                                "attribute_restrictions": None,
                                "name_form": NAME_FORMAT_URI,
                            }
                        },
                    }
                },
                "metadata": {"inline": [self.sp_metadata]},
                "key_file": key_file,
                "cert_file": cert_file,
            }
        )
        return config

    def start_login(self, browser):
        """Start a login in Waldur as the portal does; return the request id."""
        response = browser.post(reverse("saml2-login"), {"idp": KNOWN_IDP})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["binding"], "redirect")
        saml_request = parse_qs(urlparse(data["url"]).query)["SAMLRequest"][0]
        authn_request = decode_base64_and_inflate(saml_request).decode()
        return re.search(r'\bID="([^"]+)"', authn_request).group(1)

    def signed_response(
        self, entity_id, key_name, username, sign=True, in_response_to=None
    ):
        server = Server(config=self.idp_config(entity_id, key_name))
        response = server.create_authn_response(
            identity={"uid": [username]},
            in_response_to=in_response_to,
            destination=self.acs_url,
            sp_entity_id=self.sp_entity_id,
            name_id=NameID(format=NAMEID_FORMAT_TRANSIENT, text="opaque-id"),
            sign_assertion=sign,
            sign_response=sign,
            userid=username,
            authn={
                "class_ref": "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport",
                "authn_auth": entity_id,
            },
        )
        return base64.b64encode(str(response).encode()).decode()

    def post(self, saml_response):
        return self.client.post(
            self.url, {"SAMLResponse": saml_response, "RelayState": ""}
        )

    def test_response_signed_by_known_identity_provider_logs_user_in(self):
        response = self.post(self.signed_response(KNOWN_IDP, "known-idp", "alice"))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("login_completed", response["Location"])
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_response_signed_with_its_own_key_by_unknown_identity_provider_is_rejected(
        self,
    ):
        response = self.post(self.signed_response(UNKNOWN_IDP, "unknown-idp", "alice"))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("login_failed", response["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_response_claiming_known_identity_provider_but_signed_with_another_key_is_rejected(
        self,
    ):
        response = self.post(self.signed_response(KNOWN_IDP, "unknown-idp", "alice"))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("login_failed", response["Location"])

    def test_unsigned_response_is_rejected_even_if_response_signature_is_optional(
        self,
    ):
        saml_config = copy.deepcopy(settings.SAML_CONFIG)
        saml_config["service"]["sp"]["want_response_signed"] = False
        with override_settings(SAML_CONFIG=saml_config):
            response = self.post(
                self.signed_response(KNOWN_IDP, "known-idp", "alice", sign=False)
            )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("login_failed", response["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def login_started_in_waldur(self, allow_unsolicited):
        saml_config = copy.deepcopy(settings.SAML_CONFIG)
        saml_config["service"]["sp"]["allow_unsolicited"] = allow_unsolicited
        with override_settings(SAML_CONFIG=saml_config):
            request_id = self.start_login(self.client)
            # The identity provider posts the response from another site, so
            # the browser does not send the SameSite=Lax session cookie.
            identity_provider_post = Client()
            return identity_provider_post.post(
                self.url,
                {
                    "SAMLResponse": self.signed_response(
                        KNOWN_IDP, "known-idp", "alice", in_response_to=request_id
                    ),
                    "RelayState": "",
                },
            )

    def test_login_started_in_waldur_completes_without_the_session_cookie(self):
        response = self.login_started_in_waldur(allow_unsolicited=True)
        self.assertIn("login_completed", response["Location"])

    def test_login_started_in_waldur_fails_without_the_session_cookie_if_unsolicited_responses_are_refused(
        self,
    ):
        response = self.login_started_in_waldur(allow_unsolicited=False)
        self.assertIn("login_failed", response["Location"])
