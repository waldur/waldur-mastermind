from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import ImpactLevel
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


@ddt
class MaintenanceAnnouncementTemplateGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.template = self.fixture.maintenance_announcement_template
        self.url = (
            marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_list_url()
        )

    @data("staff", "service_owner")
    def test_template_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(a["uuid"] == str(self.template.uuid) for a in response.json())
        )

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_template_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            any(a["uuid"] == str(self.template.uuid) for a in response.json())
        )

    def test_template_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementOfferingTemplateGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering_template = self.fixture.maintenance_announcement_offering_template
        self.url = marketplace_factories.MaintenanceAnnouncementOfferingTemplateFactory.get_list_url()

    @data("staff", "service_owner")
    def test_template_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(a["uuid"] == str(self.offering_template.uuid) for a in response.json())
        )

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_template_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            any(a["uuid"] == str(self.offering_template.uuid) for a in response.json())
        )

    def test_template_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementTemplateCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.maintenance_announcement_template.delete()
        self.url = (
            marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_list_url()
        )
        self.offering_url = marketplace_factories.MaintenanceAnnouncementOfferingTemplateFactory.get_list_url()

    def _get_payload(self):
        return {
            "name": "Test template",
            "message": "Test template message",
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
        }

    def _get_offering_payload(self):
        maintenance_announcement_template = (
            marketplace_factories.MaintenanceAnnouncementTemplateFactory(
                service_provider=self.fixture.service_provider
            )
        )
        return {
            "maintenance_template": marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_url(
                maintenance_announcement_template
            ),
            "offering": marketplace_factories.OfferingFactory.get_url(
                self.fixture.offering
            ),
            "impact_level": ImpactLevel.FULL_OUTAGE,
            "impact_description": "Test impact template",
        }

    @data("staff", "service_owner")
    def test_creation_allowed_for_permitted_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(self._get_payload()["name"] in response.json()["name"])

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_creation_forbidden_for_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_creation_forbidden_for_unauthenticated(self):
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data("staff", "service_owner")
    def test_offering_creation_allowed_for_permitted_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.offering_url, self._get_offering_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["impact_description"], "Test impact template")

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_offering_creation_forbidden_for_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.offering_url, self._get_offering_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_creation_forbidden_for_unauthenticated(self):
        response = self.client.post(self.offering_url, self._get_offering_payload())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementTemplateDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.template = self.fixture.maintenance_announcement_template
        self.url = marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_url(
            self.template
        )

    @data("staff", "service_owner")
    def test_template_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_template_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class MaintenanceAnnouncementTemplateUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.template = self.fixture.maintenance_announcement_template
        self.url = marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_url(
            self.template
        )

    @data("staff", "service_owner")
    def test_template_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, {"message": "New template message"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_template_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, {"message": "New template message"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MaintenanceAnnouncementTemplateAffectedOfferingsTest(test.APITestCase):
    """
    Regression tests for affected_offerings always being present in template responses.

    Previously, MaintenanceAnnouncementTemplateSerializer inherited affected_offerings
    from the parent serializer which used source="affected_offerings.all" pointing to
    the MaintenanceAnnouncementOffering reverse relation — which does not exist on
    MaintenanceAnnouncementTemplate. DRF silently skipped the field (SkipField) because
    the field is read_only=True, causing it to be absent from the response entirely.
    The Python API client (waldur_api_client) calls d.pop("affected_offerings") without
    a default, raising KeyError on any instance that has templates.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.template = self.fixture.maintenance_announcement_template
        self.list_url = (
            marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_list_url()
        )
        self.detail_url = (
            marketplace_factories.MaintenanceAnnouncementTemplateFactory.get_url(
                self.template
            )
        )

    def test_affected_offerings_field_always_present_in_list(self):
        """affected_offerings must be in the response even when no offerings are linked."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(len(data) > 0)
        for item in data:
            self.assertIn(
                "affected_offerings",
                item,
                "affected_offerings field missing — would cause KeyError in waldur_api_client",
            )
            self.assertIsInstance(item["affected_offerings"], list)

    def test_affected_offerings_field_always_present_in_detail(self):
        """affected_offerings must be in the detail response even when empty."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("affected_offerings", response.json())
        self.assertEqual(response.json()["affected_offerings"], [])

    def test_affected_offerings_contains_linked_offering_templates(self):
        """When offering templates are linked, they appear in affected_offerings."""
        offering_template = self.fixture.maintenance_announcement_offering_template
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        affected = response.json()["affected_offerings"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["uuid"], str(offering_template.uuid))
