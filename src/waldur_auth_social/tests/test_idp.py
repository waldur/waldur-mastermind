import responses
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_core.structure.tests.fixtures import UserFixture


class IdentityProvidersViewSetTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = UserFixture()
        self.staff = self.fixture.staff
        self.user = self.fixture.user
        self.identity_provider = models.IdentityProvider.objects.create(
            provider="test_provider", is_active=True
        )

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
