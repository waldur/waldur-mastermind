"""Identity provider credentials, and inactive providers, are for staff. A staff
user's personal access token without `STAFF.ACCESS` acts as the user without
staff (#977).
"""

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import SECRET_PROVIDER_FIELDS
from waldur_auth_social.serializers import IdentityProviderSerializer
from waldur_core.configuration.testing import override_config
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests.fixtures import UserFixture


@override_config(PAT_ENABLED=True)
class IdentityProviderTokenScopeTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = UserFixture()
        self.staff = self.fixture.staff
        self.active = models.IdentityProvider.objects.create(
            provider="active_provider",
            is_active=True,
            client_id="client",
            client_secret="secret",
            user_claim="sub",
            attribute_mapping={"first_name": "given_name"},
        )
        self.inactive = models.IdentityProvider.objects.create(
            provider="inactive_provider", is_active=False, client_secret="secret"
        )

    def list(self):
        response = self.client.get(reverse("identity-providers-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["provider"]: item for item in response.data}

    def retrieve(self, provider):
        return self.client.get(
            reverse("identity-providers-detail", kwargs={"provider": provider.provider})
        )

    def assert_staff_view(self):
        providers = self.list()
        self.assertEqual(set(providers), {self.active.provider, self.inactive.provider})
        for field in SECRET_PROVIDER_FIELDS:
            self.assertIn(field, providers[self.active.provider])
        self.assertEqual(providers[self.active.provider]["client_secret"], "secret")
        response = self.retrieve(self.inactive)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["client_secret"], "secret")

    def assert_user_view(self):
        providers = self.list()
        self.assertEqual(set(providers), {self.active.provider})
        for field in SECRET_PROVIDER_FIELDS:
            self.assertNotIn(field, providers[self.active.provider])
        response = self.retrieve(self.active)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in SECRET_PROVIDER_FIELDS:
            self.assertNotIn(field, response.data)
        self.assertEqual(
            self.retrieve(self.inactive).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_staff_session_sees_secrets_and_inactive_providers(self):
        self.client.force_authenticate(self.staff)
        self.assert_staff_view()

    def test_staff_token_with_staff_scope_sees_secrets_and_inactive_providers(self):
        use_pat(self.client, self.staff, STAFF)
        self.assert_staff_view()

    def test_staff_token_without_staff_scope_sees_neither(self):
        use_pat(self.client, self.staff)
        self.assert_user_view()

    def test_staff_token_with_support_scope_only_sees_neither(self):
        # Support never sees identity provider credentials.
        use_pat(self.client, self.staff, SUPPORT)
        self.assert_user_view()

    def test_user_session_sees_neither(self):
        self.client.force_authenticate(self.fixture.user)
        self.assert_user_view()

    def test_anonymous_sees_neither(self):
        self.assert_user_view()

    def test_serializer_without_request_keeps_every_field(self):
        # export_auth_social serializes the providers without a request.
        data = IdentityProviderSerializer(instance=self.active).data
        for field in SECRET_PROVIDER_FIELDS:
            self.assertIn(field, data)
        self.assertEqual(data["client_secret"], "secret")
