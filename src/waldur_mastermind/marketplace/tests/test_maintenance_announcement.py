from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import ImpactLevel
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)


@ddt
class MaintenanceAnnouncementGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()

    @data("staff", "service_owner")
    def test_announcement_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(a["uuid"] == str(self.announcement.uuid) for a in response.json())
        )

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_announcement_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            any(a["uuid"] == str(self.announcement.uuid) for a in response.json())
        )

    def test_announcement_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.maintenance_announcement.delete()
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        self.offering_url = (
            marketplace_factories.MaintenanceAnnouncementOfferingFactory.get_list_url()
        )

    def _get_payload(self):
        return {
            "name": "Test maintenance",
            "message": "Test message",
            "scheduled_start": "2030-01-01T10:00:00Z",
            "scheduled_end": "2030-01-01T12:00:00Z",
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
        }

    def _get_offering_payload(self):
        maintenance_announcement = marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.fixture.service_provider
        )
        return {
            "maintenance": marketplace_factories.MaintenanceAnnouncementFactory.get_url(
                maintenance_announcement
            ),
            "offering": marketplace_factories.OfferingFactory.get_url(
                self.fixture.offering
            ),
            "impact_level": ImpactLevel.FULL_OUTAGE,
            "impact_description": "Test impact",
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
        self.assertEqual(response.json()["impact_description"], "Test impact")

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
class MaintenanceAnnouncementDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )

    @data("staff", "service_owner")
    def test_announcement_should_be_visible_to_connected_users(self, user):
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
    def test_announcement_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class MaintenanceAnnouncementUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )

    @data("staff", "service_owner")
    def test_announcement_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, {"message": "New message"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_announcement_is_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, {"message": "New message"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
