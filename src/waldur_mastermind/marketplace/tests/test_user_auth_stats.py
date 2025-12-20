import copy

from django.conf import settings
from rest_framework import status, test

from waldur_auth_social.models import IdentityProvider
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class UserAuthCountStatsTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = "/api/marketplace-stats/user_auth_method_count/"
        self.org_url = "/api/marketplace-stats/user_organization_count/"
        self.aff_url = "/api/marketplace-stats/user_affiliation_count/"

        # Patch settings
        self.orig_waldur_core = copy.deepcopy(settings.WALDUR_CORE)
        self.orig_saml2 = copy.deepcopy(settings.WALDUR_AUTH_SAML2)

        settings.WALDUR_CORE.update(
            {
                "LOCAL_IDP_NAME": "Local",
                "LOCAL_IDP_LABEL": "Local",
            }
        )
        # Use simple dictionary for SAML2 to avoid complex AttributeDict issues if present,
        # but better to update if it exists.
        # Assuming WALDUR_AUTH_SAML2 exists and is a dict.
        if not hasattr(settings, "WALDUR_AUTH_SAML2"):
            settings.WALDUR_AUTH_SAML2 = {}

        settings.WALDUR_AUTH_SAML2.update(
            {
                "NAME": "SAML2",
                "IDENTITY_PROVIDER_LABEL": "SAML",
            }
        )

        # Create IdentityProviders
        IdentityProvider.objects.create(provider="eduteams", label="eduGAIN")
        IdentityProvider.objects.create(provider="keycloak", label="Keycloak")

    def tearDown(self):
        settings.WALDUR_CORE = self.orig_waldur_core
        settings.WALDUR_AUTH_SAML2 = self.orig_saml2
        super().tearDown()

    def test_user_auth_method_count(self):
        # Create users with different registration methods
        structure_factories.UserFactory(registration_method="default")
        structure_factories.UserFactory(registration_method="default")
        structure_factories.UserFactory(registration_method="SAML2")
        structure_factories.UserFactory(registration_method="eduteams")
        structure_factories.UserFactory(registration_method="keycloak")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data

        def get_count(method_name):
            for item in data:
                print(f"Checking item: {item}")  # Debugging
                if item["method"] == method_name:
                    return item["count"]
            return 0

        # Note: Fixture creates some users too, so we use assertGreaterEqual for some
        self.assertGreaterEqual(get_count("Local"), 2)
        self.assertEqual(get_count("SAML"), 1)
        self.assertEqual(get_count("eduGAIN"), 1)
        self.assertEqual(get_count("Keycloak"), 1)

    def test_user_organization_count(self):
        structure_factories.UserFactory(organization="Org A")
        structure_factories.UserFactory(organization="Org A")
        structure_factories.UserFactory(organization="Org B")
        structure_factories.UserFactory(organization="")  # Should be excluded

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.org_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data

        def get_count(org_name):
            for item in data:
                if item["organization"] == org_name:
                    return item["count"]
            return 0

        self.assertEqual(get_count("Org A"), 2)
        self.assertEqual(get_count("Org B"), 1)

    def test_user_affiliation_count(self):
        structure_factories.UserFactory(affiliations=["student", "researcher"])
        structure_factories.UserFactory(affiliations=["student"])
        structure_factories.UserFactory(affiliations=["staff"])
        structure_factories.UserFactory(affiliations=[])

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.aff_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data

        def get_count(aff_name):
            for item in data:
                if item["affiliation"] == aff_name:
                    return item["count"]
            return 0

        self.assertEqual(get_count("student"), 2)
        self.assertEqual(get_count("researcher"), 1)
        self.assertEqual(get_count("staff"), 1)

    def test_permissions(self):
        # Regular user cannot access
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Staff can access
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Support can access
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
