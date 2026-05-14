import copy

from django.conf import settings
from rest_framework import status, test

from waldur_auth_social.models import IdentityProvider
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class UserAuthCountStatsTest(test.APITestCase):
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

    def test_user_organization_type_count(self):
        url = "/api/marketplace-stats/user_organization_type_count/"
        structure_factories.UserFactory(
            organization_type="urn:schac:organizationType:int:university"
        )
        structure_factories.UserFactory(
            organization_type="urn:schac:organizationType:int:university"
        )
        structure_factories.UserFactory(
            organization_type="urn:schac:organizationType:int:research-institution"
        )
        structure_factories.UserFactory(organization_type="")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data

        def get_count(org_type):
            for item in data:
                if item["organization_type"] == org_type:
                    return item["count"]
            return 0

        self.assertEqual(get_count("urn:schac:organizationType:int:university"), 2)
        self.assertEqual(
            get_count("urn:schac:organizationType:int:research-institution"), 1
        )
        # Empty organization types should be excluded
        self.assertIsNone(get_count("") or None)

    def test_user_organization_type_count_permissions(self):
        url = "/api/marketplace-stats/user_organization_type_count/"

        # Regular user cannot access
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Staff can access
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Support can access
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_job_title_count(self):
        url = "/api/marketplace-stats/user_job_title_count/"
        structure_factories.UserFactory(job_title="Software Engineer")
        structure_factories.UserFactory(job_title="software engineer")  # normalized
        structure_factories.UserFactory(job_title="  Software Engineer  ")  # trimmed
        structure_factories.UserFactory(job_title="Data Scientist")
        structure_factories.UserFactory(job_title="")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data

        def get_count(title):
            for item in data:
                if item["job_title"] == title:
                    return item["count"]
            return 0

        # All three "Software Engineer" variants should be normalized to same key
        self.assertEqual(get_count("software engineer"), 3)
        self.assertEqual(get_count("data scientist"), 1)
        # Empty job titles should be excluded
        self.assertIsNone(get_count("") or None)

    def test_user_job_title_count_permissions(self):
        url = "/api/marketplace-stats/user_job_title_count/"

        # Regular user cannot access
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Staff can access
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Support can access
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class UserAffiliationDetailsTest(test.APITestCase):
    url = "/api/marketplace-stats/user_affiliation_details/"

    def setUp(self):
        super().setUp()
        self.fixture = structure_fixtures.ProjectFixture()
        # Two users sharing one affiliation — gives us a count > 1 row.
        structure_factories.UserFactory(
            affiliations=[
                "urn:mace:terena.org:schac:homeOrganization:helsinki.fi",
                "urn:schac:homeOrganizationType:int:university",
            ]
        )
        structure_factories.UserFactory(
            affiliations=[
                "urn:mace:terena.org:schac:homeOrganization:helsinki.fi",
            ]
        )
        structure_factories.UserFactory(
            affiliations=[
                "urn:mace:terena.org:schac:homeOrganization:ut.ee",
                "faculty",
            ]
        )
        structure_factories.UserFactory(affiliations=["staff@cern.ch"])
        self.client.force_authenticate(self.fixture.staff)

    def _by_affiliation(self, items):
        return {item["affiliation"]: item for item in items}

    def test_parses_organization_country_and_category(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_aff = self._by_affiliation(response.data)

        helsinki = by_aff["urn:mace:terena.org:schac:homeOrganization:helsinki.fi"]
        self.assertEqual(helsinki["organization"], "helsinki.fi")
        self.assertEqual(helsinki["country"], "fi")
        self.assertEqual(helsinki["category"], "home-organization")
        self.assertEqual(helsinki["count"], 2)

        ut = by_aff["urn:mace:terena.org:schac:homeOrganization:ut.ee"]
        self.assertEqual(ut["country"], "ee")
        self.assertEqual(ut["organization"], "ut.ee")

        ot = by_aff["urn:schac:homeOrganizationType:int:university"]
        self.assertEqual(ot["category"], "organization-type")
        self.assertEqual(ot["country"], "int")

        cern = by_aff["staff@cern.ch"]
        self.assertEqual(cern["organization"], "cern.ch")
        self.assertEqual(cern["country"], "ch")
        self.assertEqual(cern["category"], "eduperson")

    def test_filter_by_country(self):
        response = self.client.get(self.url, {"country": "fi"})
        countries = {row["country"] for row in response.data}
        self.assertEqual(countries, {"fi"})

    def test_filter_by_category(self):
        response = self.client.get(self.url, {"category": "home-organization"})
        categories = {row["category"] for row in response.data}
        self.assertEqual(categories, {"home-organization"})

    def test_filter_by_organization(self):
        response = self.client.get(self.url, {"organization": "ut.ee"})
        orgs = {row["organization"] for row in response.data}
        self.assertEqual(orgs, {"ut.ee"})

    def test_search_substring(self):
        response = self.client.get(self.url, {"search": "helsinki"})
        self.assertEqual(len(response.data), 1)
        self.assertIn("helsinki", response.data[0]["affiliation"])

    def test_ordering_by_count_descending_by_default(self):
        response = self.client.get(self.url)
        counts = [row["count"] for row in response.data]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_ordering_by_organization(self):
        response = self.client.get(self.url, {"o": "organization"})
        orgs = [row["organization"] for row in response.data if row["organization"]]
        self.assertEqual(orgs, sorted(orgs))

    def test_pagination_returns_x_result_count_header(self):
        response = self.client.get(self.url, {"page_size": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertIn("X-Result-Count", response.headers)

    def test_pagination_second_page(self):
        first = self.client.get(self.url, {"page_size": 2, "page": 1})
        second = self.client.get(self.url, {"page_size": 2, "page": 2})
        total = int(first.headers["X-Result-Count"])
        page1_keys = {row["affiliation"] for row in first.data}
        page2_keys = {row["affiliation"] for row in second.data}
        self.assertFalse(page1_keys & page2_keys)
        self.assertEqual(len(page1_keys) + len(page2_keys), min(total, 4))

    def test_legacy_count_endpoint_still_flat(self):
        # The original /user_affiliation_count/ stays untouched — the
        # affiliations chart and summary widgets depend on it.
        legacy = self.client.get("/api/marketplace-stats/user_affiliation_count/")
        self.assertEqual(legacy.status_code, status.HTTP_200_OK)
        self.assertNotIn("X-Result-Count", legacy.headers)
        rows = list(legacy.data)
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("affiliation", row)
            self.assertIn("count", row)
            self.assertNotIn("organization", row)

    def test_permissions(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
