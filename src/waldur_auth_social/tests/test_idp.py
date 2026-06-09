import socket
from unittest import mock

import responses
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_core.structure.tests.fixtures import UserFixture


def _addrinfo(ip):
    """Build a socket.getaddrinfo-style result resolving to a single IP."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


class IdentityProvidersViewSetTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = UserFixture()
        self.staff = self.fixture.staff
        self.user = self.fixture.user
        self.identity_provider = models.IdentityProvider.objects.create(
            provider="test_provider", is_active=True
        )
        # The SSRF guard resolves the discovery host; pin it to a public IP so
        # tests stay deterministic and offline. Negative tests override this.
        resolve_patcher = mock.patch(
            "waldur_auth_social.utils.socket.getaddrinfo",
            return_value=_addrinfo("93.184.216.34"),
        )
        self.mock_getaddrinfo = resolve_patcher.start()
        self.addCleanup(resolve_patcher.stop)

    def get_list_url(self):
        return reverse("identity-providers-list")

    def get_detail_url(self):
        return reverse(
            "identity-providers-detail",
            kwargs={"provider": self.identity_provider.provider},
        )

    def _mock_openid_configuration(self, discovery_url, base_url=None):
        """Helper method to mock OpenID configuration responses."""
        if base_url is None:
            base_url = discovery_url.replace("/.well-known/openid-configuration", "")

        responses.add(
            responses.GET,
            discovery_url,
            json={
                "userinfo_endpoint": f"{base_url}/userinfo",
                "token_endpoint": f"{base_url}/token",
                "authorization_endpoint": f"{base_url}/auth",
                "end_session_endpoint": f"{base_url}/logout",
            },
            status=200,
        )

    def _get_base_provider_payload(
        self, provider_type=ProviderChoices.KEYCLOAK, discovery_url=None, **kwargs
    ):
        """Helper method to create base provider payload."""
        if discovery_url is None:
            discovery_url = "https://example.com/.well-known/openid-configuration"

        payload = {
            "provider": provider_type,
            "discovery_url": discovery_url,
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "label": "Test IdP",
        }
        payload.update(kwargs)
        return payload

    def test_staff_can_list_all_providers(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.get_list_url())
        self.assertEqual(len(response.data), 1)

    def test_non_staff_can_list_active_providers(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.get_list_url())
        self.assertEqual(len(response.data), 1)

    def test_non_staff_cannot_list_inactive_providers(self):
        self.identity_provider.is_active = False
        self.identity_provider.save()
        self.client.force_authenticate(self.user)
        response = self.client.get(self.get_list_url())
        self.assertEqual(len(response.data), 0)

    @responses.activate
    def test_staff_can_create_provider(self):
        self._mock_openid_configuration(
            "https://example.com/.well-known/openid-configuration"
        )
        self.client.force_authenticate(self.staff)

        payload = self._get_base_provider_payload()
        response = self.client.post(self.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_non_staff_cannot_create_provider(self):
        self.client.force_authenticate(self.user)
        payload = self._get_base_provider_payload(provider_type="new_provider")
        response = self.client.post(self.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @responses.activate
    def test_tara_provider_creation_with_defaults(self):
        discovery_url = "https://tara-test.ria.ee/.well-known/openid-configuration"
        self._mock_openid_configuration(discovery_url)
        self.client.force_authenticate(self.staff)

        payload = self._get_base_provider_payload(
            provider_type=ProviderChoices.TARA, discovery_url=discovery_url
        )
        response = self.client.post(self.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self._assert_provider_defaults(ProviderChoices.TARA)

    @responses.activate
    def test_tara_provider_creation_with_custom_attribute_mapping(self):
        discovery_url = "https://tara-test.ria.ee/.well-known/openid-configuration"
        self._mock_openid_configuration(discovery_url)
        self.client.force_authenticate(self.staff)

        custom_overrides = {
            "user_field": "overridden_user_field",
            "user_claim": "overridden_user_claim",
            "attribute_mapping": {"email": "overridden_email"},
            "extra_fields": "overridden_extra_fields",
        }

        payload = self._get_base_provider_payload(
            provider_type=ProviderChoices.TARA,
            discovery_url=discovery_url,
            **custom_overrides,
        )
        response = self.client.post(self.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self._assert_provider_custom_values(
            ProviderChoices.TARA,
            {
                **custom_overrides,
                "attribute_mapping": {
                    "email": "overridden_email",
                    "first_name": "given_name",
                    "last_name": "family_name",
                    "civil_number": "sub",
                },
            },
        )

    def _assert_provider_defaults(self, provider_type):
        """Helper method to assert provider has default values."""
        provider = models.IdentityProvider.objects.get(provider=provider_type)
        defaults = PROVIDER_DEFAULTS[provider_type]

        self.assertEqual(provider.user_field, defaults["user_field"])
        self.assertEqual(provider.user_claim, defaults["user_claim"])
        self.assertEqual(provider.attribute_mapping, defaults["attribute_mapping"])
        self.assertEqual(provider.extra_fields, defaults["extra_fields"])

    def _assert_provider_custom_values(self, provider_type, custom_values):
        """Helper method to assert provider has custom values."""
        provider = models.IdentityProvider.objects.get(provider=provider_type)

        self.assertEqual(provider.user_field, custom_values["user_field"])
        self.assertEqual(provider.user_claim, custom_values["user_claim"])
        self.assertEqual(provider.attribute_mapping, custom_values["attribute_mapping"])
        self.assertEqual(provider.extra_fields, custom_values["extra_fields"])

    def get_discover_metadata_url(self):
        return reverse("identity-providers-discover-metadata")

    def get_generate_mapping_url(self):
        return reverse("identity-providers-generate-mapping")

    def _mock_openid_configuration_with_claims(
        self, discovery_url, claims_supported=None, scopes_supported=None
    ):
        """Helper method to mock OpenID configuration with claims and scopes."""
        base_url = discovery_url.replace("/.well-known/openid-configuration", "")

        if claims_supported is None:
            claims_supported = [
                "sub",
                "email",
                "given_name",
                "family_name",
                "preferred_username",
                "name",
            ]

        if scopes_supported is None:
            scopes_supported = ["openid", "profile", "email"]

        responses.add(
            responses.GET,
            discovery_url,
            json={
                "userinfo_endpoint": f"{base_url}/userinfo",
                "token_endpoint": f"{base_url}/token",
                "authorization_endpoint": f"{base_url}/auth",
                "end_session_endpoint": f"{base_url}/logout",
                "jwks_uri": f"{base_url}/jwks",
                "claims_supported": claims_supported,
                "scopes_supported": scopes_supported,
            },
            status=200,
        )

    @responses.activate
    def test_discover_metadata_returns_claims_and_suggestions(self):
        discovery_url = "https://example.com/.well-known/openid-configuration"
        self._mock_openid_configuration_with_claims(
            discovery_url,
            claims_supported=[
                "sub",
                "email",
                "given_name",
                "family_name",
                "schacHomeOrganization",
                "phone_number",
            ],
            scopes_supported=["openid", "profile", "email", "phone"],
        )

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": discovery_url},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Check claims are returned
        self.assertIn("claims_supported", response.data)
        self.assertIn("email", response.data["claims_supported"])
        self.assertIn("given_name", response.data["claims_supported"])

        # Check scopes are returned
        self.assertIn("scopes_supported", response.data)
        self.assertIn("openid", response.data["scopes_supported"])

        # Check endpoints are returned
        self.assertIn("endpoints", response.data)
        self.assertIn("authorization_endpoint", response.data["endpoints"])
        self.assertIn("token_endpoint", response.data["endpoints"])
        self.assertIn("userinfo_endpoint", response.data["endpoints"])

        # Check waldur_fields suggestions
        self.assertIn("waldur_fields", response.data)
        field_names = [f["field"] for f in response.data["waldur_fields"]]
        self.assertIn("first_name", field_names)
        self.assertIn("email", field_names)

        # Check that available_claims are populated for fields with matching claims
        email_field = next(
            f for f in response.data["waldur_fields"] if f["field"] == "email"
        )
        self.assertIn("email", email_field["available_claims"])

        first_name_field = next(
            f for f in response.data["waldur_fields"] if f["field"] == "first_name"
        )
        self.assertIn("given_name", first_name_field["available_claims"])

        # Check suggested scopes
        self.assertIn("suggested_scopes", response.data)
        self.assertIn("openid", response.data["suggested_scopes"])
        self.assertIn("email", response.data["suggested_scopes"])

    @responses.activate
    def test_discover_metadata_handles_empty_claims(self):
        """Test that discovery works even when claims_supported is not provided."""
        discovery_url = "https://example.com/.well-known/openid-configuration"
        base_url = "https://example.com"

        # Mock without claims_supported (optional in OIDC spec)
        responses.add(
            responses.GET,
            discovery_url,
            json={
                "userinfo_endpoint": f"{base_url}/userinfo",
                "token_endpoint": f"{base_url}/token",
                "authorization_endpoint": f"{base_url}/auth",
            },
            status=200,
        )

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": discovery_url},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["claims_supported"], [])
        self.assertEqual(response.data["scopes_supported"], [])

    @responses.activate
    def test_discover_metadata_fails_for_invalid_url(self):
        import requests as req_lib

        discovery_url = "https://invalid.example.com/.well-known/openid-configuration"

        responses.add(
            responses.GET,
            discovery_url,
            body=req_lib.exceptions.ConnectionError("Connection refused"),
        )

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": discovery_url},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_discover_metadata_requires_authentication(self):
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": "https://example.com/.well-known/openid-configuration"},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_discover_metadata_requires_staff(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": "https://example.com/.well-known/openid-configuration"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_generate_mapping_requires_staff(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            self.get_generate_mapping_url(),
            {"discovery_url": "https://example.com/.well-known/openid-configuration"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @responses.activate
    def test_generate_mapping_returns_suggested_configuration(self):
        discovery_url = "https://example.com/.well-known/openid-configuration"
        self._mock_openid_configuration_with_claims(
            discovery_url,
            claims_supported=[
                "sub",
                "email",
                "given_name",
                "family_name",
                "schacHomeOrganization",
            ],
            scopes_supported=["openid", "profile", "email"],
        )

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_generate_mapping_url(),
            {"discovery_url": discovery_url},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Check attribute_mapping is returned
        self.assertIn("attribute_mapping", response.data)
        mapping = response.data["attribute_mapping"]

        # Should have mappings for fields with available claims
        self.assertIn("email", mapping)
        self.assertIn("first_name", mapping)
        self.assertIn("last_name", mapping)
        self.assertIn("organization", mapping)

        # Check extra_scope is returned
        self.assertIn("extra_scope", response.data)
        self.assertIn("profile", response.data["extra_scope"])
        self.assertIn("email", response.data["extra_scope"])

    # --- SSRF guard on the discovery URL fetch ---

    def _assert_discovery_blocked(self, url, resolved_ip):
        self.mock_getaddrinfo.return_value = _addrinfo(resolved_ip)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": url},
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("discovery_url", response.data)

    def test_discover_metadata_blocks_cloud_metadata_endpoint(self):
        # 169.254.169.254 is link-local — the classic SSRF target.
        self._assert_discovery_blocked(
            "https://metadata.attacker.example/.well-known/openid-configuration",
            "169.254.169.254",
        )

    @responses.activate
    def test_discover_metadata_allows_in_cluster_private_idp(self):
        # An in-cluster Keycloak on a ClusterIP (RFC-1918) is a legitimate IdP
        # location and must NOT be blocked.
        self.mock_getaddrinfo.return_value = _addrinfo("10.96.0.10")
        discovery_url = (
            "https://keycloak.auth.svc.cluster.local/.well-known/openid-configuration"
        )
        self._mock_openid_configuration_with_claims(discovery_url)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_discover_metadata_url(),
            {"discovery_url": discovery_url},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_discover_metadata_blocks_loopback(self):
        self._assert_discovery_blocked(
            "https://localhost.attacker.example/.well-known/openid-configuration",
            "127.0.0.1",
        )

    def test_discover_metadata_blocks_ipv4_mapped_ipv6_metadata(self):
        # ::ffff:169.254.169.254 must be unwrapped and rejected too.
        self._assert_discovery_blocked(
            "https://sneaky.example/.well-known/openid-configuration",
            "::ffff:169.254.169.254",
        )

    def test_generate_mapping_blocks_cloud_metadata_endpoint(self):
        # Covers the generate_mapping fetch sink with a genuinely blocked
        # (link-local metadata) address.
        self.mock_getaddrinfo.return_value = _addrinfo("169.254.169.254")
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.get_generate_mapping_url(),
            {
                "discovery_url": "https://metadata.attacker.example/.well-known/openid-configuration"
            },
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_provider_blocks_metadata_discovery_url(self):
        self.mock_getaddrinfo.return_value = _addrinfo("169.254.169.254")
        self.client.force_authenticate(self.staff)
        payload = self._get_base_provider_payload(
            provider_type=ProviderChoices.KEYCLOAK,
            discovery_url="https://idp.internal.example/.well-known/openid-configuration",
        )
        response = self.client.post(self.get_list_url(), payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertFalse(
            models.IdentityProvider.objects.filter(
                provider=ProviderChoices.KEYCLOAK
            ).exists()
        )
