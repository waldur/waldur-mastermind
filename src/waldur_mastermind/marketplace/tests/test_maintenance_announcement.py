from datetime import timedelta

from ddt import data, ddt
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    ImpactLevel,
    MaintenanceState,
    MaintenanceType,
)
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)

MANAGE_DENIED_DETAIL = (
    "You do not have permission to manage maintenance announcements "
    "for this service provider."
)
ACTION_DENIED_DETAIL = "You do not have permission to perform this action."


def _assert_permission_denied(response, detail):
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.data
    assert response.data.get("detail") == detail, response.data


@ddt
class MaintenanceAnnouncementGetTest(test.APITestCase):
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
class MaintenanceAnnouncementCreateTest(test.APITestCase):
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
        _assert_permission_denied(response, MANAGE_DENIED_DETAIL)

    def test_creation_forbidden_for_related_user_without_permission(self):
        user = structure_factories.UserFactory()
        self.fixture.offering_customer.add_user(user, CustomerRole.SUPPORT)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        _assert_permission_denied(response, MANAGE_DENIED_DETAIL)

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
        _assert_permission_denied(response, MANAGE_DENIED_DETAIL)

    def test_offering_creation_forbidden_for_unauthenticated(self):
        response = self.client.post(self.offering_url, self._get_offering_payload())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementPermissionTest(test.APITestCase):
    """Cover MANAGE_MAINTENANCE_ANNOUNCEMENT paths for connected users."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.offering_link = self.fixture.maintenance_announcement_offering
        self.list_url = (
            marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        )
        self.detail_url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )
        self.offering_list_url = (
            marketplace_factories.MaintenanceAnnouncementOfferingFactory.get_list_url()
        )
        self.offering_detail_url = (
            marketplace_factories.MaintenanceAnnouncementOfferingFactory.get_url(
                self.offering_link
            )
        )
        self.related_without_perm = structure_factories.UserFactory()
        self.fixture.offering_customer.add_user(
            self.related_without_perm, CustomerRole.SUPPORT
        )

    def _create_payload(self):
        return {
            "name": "Permission test maintenance",
            "message": "Test message",
            "scheduled_start": "2030-01-01T10:00:00Z",
            "scheduled_end": "2030-01-01T12:00:00Z",
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
        }

    def _offering_payload(self):
        return {
            "maintenance": marketplace_factories.MaintenanceAnnouncementFactory.get_url(
                self.announcement
            ),
            "offering": marketplace_factories.OfferingFactory.get_url(
                self.fixture.offering
            ),
            "impact_level": ImpactLevel.FULL_OUTAGE,
            "impact_description": "Impact",
        }

    def test_related_user_without_permission_can_list_and_retrieve(self):
        self.client.force_authenticate(self.related_without_perm)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(a["uuid"] == str(self.announcement.uuid) for a in response.json())
        )
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_related_user_without_permission_cannot_update(self):
        self.client.force_authenticate(self.related_without_perm)
        response = self.client.patch(self.detail_url, {"message": "Nope"})
        _assert_permission_denied(response, ACTION_DENIED_DETAIL)

    def test_related_user_without_permission_cannot_delete(self):
        self.client.force_authenticate(self.related_without_perm)
        response = self.client.delete(self.detail_url)
        _assert_permission_denied(response, ACTION_DENIED_DETAIL)

    @data(
        "schedule",
        "unschedule",
        "start_maintenance",
        "complete_maintenance",
        "cancel_maintenance",
    )
    def test_related_user_without_permission_cannot_change_state(self, action):
        state_by_action = {
            "schedule": MaintenanceState.DRAFT,
            "unschedule": MaintenanceState.SCHEDULED,
            "start_maintenance": MaintenanceState.SCHEDULED,
            "complete_maintenance": MaintenanceState.IN_PROGRESS,
            "cancel_maintenance": MaintenanceState.DRAFT,
        }
        self.announcement.state = state_by_action[action]
        self.announcement.save()
        self.client.force_authenticate(self.related_without_perm)
        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action=action
        )
        response = self.client.post(url)
        _assert_permission_denied(response, ACTION_DENIED_DETAIL)

    def test_related_user_without_permission_cannot_manage_offering_link(self):
        self.client.force_authenticate(self.related_without_perm)
        response = self.client.post(self.offering_list_url, self._offering_payload())
        _assert_permission_denied(response, MANAGE_DENIED_DETAIL)

        response = self.client.patch(
            self.offering_detail_url, {"impact_description": "Nope"}
        )
        _assert_permission_denied(response, ACTION_DENIED_DETAIL)

        response = self.client.delete(self.offering_detail_url)
        _assert_permission_denied(response, ACTION_DENIED_DETAIL)

    @data("service_owner", "service_manager")
    def test_permitted_roles_can_create_and_update(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.list_url, self._create_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        response = self.client.patch(self.detail_url, {"message": "Updated"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("service_owner", "service_manager")
    def test_permitted_roles_can_schedule(self, user):
        user = getattr(self.fixture, user)
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()
        self.client.force_authenticate(user)
        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="schedule"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


@ddt
class MaintenanceAnnouncementDeleteTest(test.APITestCase):
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
class MaintenanceAnnouncementUpdateTest(test.APITestCase):
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


@ddt
class MaintenanceAnnouncementUpdateStateTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )
        self.client.force_authenticate(self.fixture.staff)

    def _set_state(self, state):
        self.announcement.state = state
        self.announcement.save()

    @data(
        MaintenanceState.DRAFT,
        MaintenanceState.SCHEDULED,
        MaintenanceState.IN_PROGRESS,
    )
    def test_update_allowed_while_active(self, state):
        self._set_state(state)
        response = self.client.patch(self.url, {"message": "Updated message"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.message, "Updated message")

    @data(MaintenanceState.COMPLETED, MaintenanceState.CANCELLED)
    def test_update_forbidden_in_terminal_states(self, state):
        self._set_state(state)
        response = self.client.patch(self.url, {"message": "Updated message"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.announcement.refresh_from_db()
        self.assertNotEqual(self.announcement.message, "Updated message")

    def test_internal_notes_can_be_updated_while_in_progress(self):
        self._set_state(MaintenanceState.IN_PROGRESS)
        response = self.client.patch(
            self.url, {"internal_notes": "Ended early: work finished ahead of schedule"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.announcement.refresh_from_db()
        self.assertEqual(
            self.announcement.internal_notes,
            "Ended early: work finished ahead of schedule",
        )


@ddt
class MaintenanceAnnouncementScheduleTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="schedule"
        )

    @data("staff", "service_owner")
    def test_schedule_allowed_for_permitted_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in DRAFT state
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the announcement is now in SCHEDULED state
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.state, MaintenanceState.SCHEDULED)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_schedule_forbidden_for_unauthorized_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in DRAFT state
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_schedule_fails_when_not_in_draft_state(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to SCHEDULED state (not DRAFT)
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unauthenticated_user_cannot_schedule(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementUnscheduleTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="unschedule"
        )

    @data("staff", "service_owner")
    def test_unschedule_allowed_for_permitted_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in SCHEDULED state
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the announcement is now in DRAFT state
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.state, MaintenanceState.DRAFT)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_unschedule_forbidden_for_unauthorized_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in SCHEDULED state
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unschedule_fails_when_not_in_scheduled_state(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to DRAFT state (not SCHEDULED)
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unschedule_fails_when_in_progress(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to IN_PROGRESS state (cannot be unscheduled)
        self.announcement.state = MaintenanceState.IN_PROGRESS
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unschedule_fails_when_completed(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to COMPLETED state (cannot be unscheduled)
        self.announcement.state = MaintenanceState.COMPLETED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unschedule_fails_when_cancelled(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to CANCELLED state (cannot be unscheduled)
        self.announcement.state = MaintenanceState.CANCELLED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unauthenticated_user_cannot_unschedule(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementStartTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="start_maintenance"
        )

    @data("staff", "service_owner")
    def test_start_allowed_for_permitted_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in SCHEDULED state
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the announcement is now in IN_PROGRESS state
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.state, MaintenanceState.IN_PROGRESS)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_start_forbidden_for_unauthorized_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in SCHEDULED state
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_start_fails_when_not_in_scheduled_state(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to DRAFT state (not SCHEDULED)
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unauthenticated_user_cannot_start(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementCompleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="complete_maintenance"
        )

    @data("staff", "service_owner")
    def test_complete_allowed_for_permitted_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in IN_PROGRESS state
        self.announcement.state = MaintenanceState.IN_PROGRESS
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the announcement is now in COMPLETED state
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.state, MaintenanceState.COMPLETED)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_complete_forbidden_for_unauthorized_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in IN_PROGRESS state
        self.announcement.state = MaintenanceState.IN_PROGRESS
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_complete_fails_when_not_in_progress_state(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to SCHEDULED state (not IN_PROGRESS)
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unauthenticated_user_cannot_complete(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class MaintenanceAnnouncementCancelTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="cancel_maintenance"
        )

    @data("staff", "service_owner")
    def test_cancel_allowed_from_draft_state(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in DRAFT state
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the announcement is now in CANCELLED state
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.state, MaintenanceState.CANCELLED)

    @data("staff", "service_owner")
    def test_cancel_allowed_from_scheduled_state(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in SCHEDULED state
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the announcement is now in CANCELLED state
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.state, MaintenanceState.CANCELLED)

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_cancel_forbidden_for_unauthorized_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Ensure the announcement is in DRAFT state
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cancel_fails_when_in_progress(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to IN_PROGRESS state (cannot be cancelled)
        self.announcement.state = MaintenanceState.IN_PROGRESS
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cancel_fails_when_completed(self):
        self.client.force_authenticate(self.fixture.staff)

        # Set announcement to COMPLETED state (cannot be cancelled)
        self.announcement.state = MaintenanceState.COMPLETED
        self.announcement.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unauthenticated_user_cannot_cancel(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class PublicMaintenanceAnnouncementViewSetTest(test.APITestCase):
    """Test the public maintenance announcement viewset that allows anonymous access."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.draft_announcement = self.fixture.maintenance_announcement
        self.draft_announcement.state = MaintenanceState.DRAFT
        self.draft_announcement.save()
        # Create announcements in different states
        self.scheduled_announcement = (
            marketplace_factories.MaintenanceAnnouncementFactory(
                service_provider=self.fixture.service_provider,
                state=MaintenanceState.SCHEDULED,
                name="Scheduled Maintenance",
                scheduled_start="2030-01-01T10:00:00Z",
                scheduled_end="2030-01-01T12:00:00Z",
            )
        )

        self.in_progress_announcement = (
            marketplace_factories.MaintenanceAnnouncementFactory(
                service_provider=self.fixture.service_provider,
                state=MaintenanceState.IN_PROGRESS,
                name="In Progress Maintenance",
                scheduled_start="2030-01-01T08:00:00Z",
                scheduled_end="2030-01-01T14:00:00Z",
            )
        )

        self.completed_announcement = (
            marketplace_factories.MaintenanceAnnouncementFactory(
                service_provider=self.fixture.service_provider,
                state=MaintenanceState.COMPLETED,
                name="Completed Maintenance",
                scheduled_start="2020-01-01T10:00:00Z",
                scheduled_end="2020-01-01T12:00:00Z",
            )
        )

        self.url = "/api/public-maintenance-announcements/"
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(self.user)

    def test_anonymous_users_can_list_public_announcements(self):
        """Anonymous users should be able to list scheduled, in-progress, and completed announcements."""

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(len(data), 3)
        announcement_uuids = [a["uuid"] for a in data]
        self.assertIn(str(self.scheduled_announcement.uuid), announcement_uuids)
        self.assertIn(str(self.in_progress_announcement.uuid), announcement_uuids)
        self.assertIn(str(self.completed_announcement.uuid), announcement_uuids)

        self.assertNotIn(str(self.draft_announcement.uuid), announcement_uuids)

    def test_anonymous_users_can_retrieve_public_announcement_details(self):
        """Anonymous users should be able to retrieve details of public announcements."""
        url = f"{self.url}{self.scheduled_announcement.uuid}/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["name"], "Scheduled Maintenance")
        self.assertIn("message", data)
        self.assertIn("scheduled_start", data)
        self.assertIn("scheduled_end", data)
        self.assertIn("maintenance_type", data)
        self.assertIn("maintenance_type_display", data)
        self.assertIn("state", data)

        self.assertNotIn("created_by", data)
        self.assertNotIn("service_provider", data)

    def test_public_endpoint_filters_by_state_correctly(self):
        """The public endpoint should only show announcements in SCHEDULED, IN_PROGRESS, or COMPLETED states."""
        # Create a cancelled announcement (should not be visible)
        cancelled_announcement = marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.fixture.service_provider,
            state=MaintenanceState.CANCELLED,
            name="Cancelled Maintenance",
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        announcement_uuids = [a["uuid"] for a in data]

        # Should still only see 3 announcements (not the cancelled one)
        self.assertEqual(len(data), 3)
        self.assertNotIn(str(cancelled_announcement.uuid), announcement_uuids)
        self.assertNotIn(str(self.draft_announcement.uuid), announcement_uuids)

    def test_anonymous_users_cannot_create_maintenance_announcements(self):
        """Anonymous users should not be able to create maintenance announcements via the public endpoint."""
        payload = {
            "name": "Test Maintenance",
            "message": "Test Message",
            "scheduled_start": "2030-01-01T10:00:00Z",
            "scheduled_end": "2030-01-01T12:00:00Z",
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        detail_url = f"{self.url}{self.scheduled_announcement.uuid}/"
        response = self.client.put(detail_url, payload)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        response = self.client.patch(detail_url, {"message": "Unauthorized update"})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


@ddt
class MaintenanceAnnouncementInternalNotesTest(test.APITestCase):
    """Test internal_notes field visibility based on user permissions."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.announcement.internal_notes = "Secret internal notes for staff only"
        self.announcement.save()
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )

        # Create a support user
        self.support_user = structure_factories.UserFactory(is_support=True)

    @data("staff", "service_owner")
    def test_internal_notes_visible_to_authorized_users(self, user):
        """Staff and service provider users should see internal_notes."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("internal_notes", response.json())
        self.assertEqual(
            response.json()["internal_notes"], "Secret internal notes for staff only"
        )

    def test_internal_notes_visible_to_support_users(self):
        """Support users should see internal_notes."""
        self.client.force_authenticate(self.support_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("internal_notes", response.json())
        self.assertEqual(
            response.json()["internal_notes"], "Secret internal notes for staff only"
        )

    @data(
        "admin",
        "manager",
        "offering_admin",
        "offering_manager",
        "owner",
        "customer_support",
    )
    def test_internal_notes_hidden_from_unauthorized_users(self, user):
        """Unauthorized users should not see internal_notes field when they can access the announcement."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        # These users might not have access to the announcement at all due to existing permissions
        # If they get 404, that's expected behavior for the existing permission system
        if response.status_code == status.HTTP_404_NOT_FOUND:
            # This is expected - user doesn't have access to this announcement
            return
        elif response.status_code == status.HTTP_200_OK:
            # If they do have access, internal_notes should be hidden
            self.assertNotIn("internal_notes", response.json())
        else:
            self.fail(f"Unexpected status code: {response.status_code}")

    def test_internal_notes_hidden_from_anonymous_users(self):
        """Anonymous users should not see internal_notes field."""
        # No authentication
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data("staff", "service_owner")
    def test_internal_notes_can_be_created_by_authorized_users(self, user):
        """Staff and service provider users should be able to create internal_notes."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "Test maintenance with internal notes",
            "message": "Public message",
            "internal_notes": "Private internal information",
            "scheduled_start": "2030-01-01T10:00:00Z",
            "scheduled_end": "2030-01-01T12:00:00Z",
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
        }

        list_url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        response = self.client.post(list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify internal_notes is present in response
        self.assertIn("internal_notes", response.json())
        self.assertEqual(
            response.json()["internal_notes"], "Private internal information"
        )

    def test_internal_notes_can_be_created_by_support_users(self):
        """Support users should be able to create internal_notes."""
        self.client.force_authenticate(self.support_user)

        payload = {
            "name": "Test maintenance with internal notes",
            "message": "Public message",
            "internal_notes": "Support user notes",
            "scheduled_start": "2030-01-01T10:00:00Z",
            "scheduled_end": "2030-01-01T12:00:00Z",
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
        }

        list_url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        response = self.client.post(list_url, payload)
        # Global support is not staff and has no manage permission on the SP.
        _assert_permission_denied(response, MANAGE_DENIED_DETAIL)

    @data("staff", "service_owner")
    def test_internal_notes_can_be_updated_by_authorized_users(self, user):
        """Staff and service provider users should be able to update internal_notes."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.patch(
            self.url, {"internal_notes": "Updated internal notes"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify updated internal_notes
        self.assertIn("internal_notes", response.json())
        self.assertEqual(response.json()["internal_notes"], "Updated internal notes")

        # Verify in database
        self.announcement.refresh_from_db()
        self.assertEqual(self.announcement.internal_notes, "Updated internal notes")

    def test_internal_notes_can_be_updated_by_support_users(self):
        """Support users without manage permission cannot update announcements."""
        self.client.force_authenticate(self.support_user)

        response = self.client.patch(
            self.url, {"internal_notes": "Support updated notes"}
        )
        _assert_permission_denied(response, ACTION_DENIED_DETAIL)

    def test_internal_notes_in_list_view_for_authorized_users(self):
        """Internal notes should be included in list view for authorized users."""
        self.client.force_authenticate(self.fixture.staff)

        list_url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        announcements = response.json()
        # Find our test announcement in the list
        test_announcement = next(
            (a for a in announcements if a["uuid"] == str(self.announcement.uuid)), None
        )

        self.assertIsNotNone(test_announcement)
        self.assertIn("internal_notes", test_announcement)
        self.assertEqual(
            test_announcement["internal_notes"], "Secret internal notes for staff only"
        )

    def test_internal_notes_hidden_in_list_view_for_unauthorized_users(self):
        """Internal notes should be hidden in list view for unauthorized users."""
        self.client.force_authenticate(self.fixture.admin)

        list_url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        announcements = response.json()
        # Find our test announcement in the list (may not be visible due to permissions)
        test_announcement = next(
            (a for a in announcements if a["uuid"] == str(self.announcement.uuid)), None
        )

        if test_announcement is not None:
            # If the announcement is visible, internal_notes should be hidden
            self.assertNotIn("internal_notes", test_announcement)
        # If test_announcement is None, the user doesn't have access to this announcement,
        # which is expected behavior based on existing permissions

    def test_internal_notes_not_included_in_public_api(self):
        """Internal notes should never be included in public API responses."""
        public_url = "/api/public-maintenance-announcements/"

        # Set announcement to scheduled state to make it visible in public API
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()

        # Test unauthenticated request to public API
        self.client.logout()
        response = self.client.get(public_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        announcements = response.json()
        test_announcement = next(
            (a for a in announcements if a["uuid"] == str(self.announcement.uuid)), None
        )

        self.assertIsNotNone(test_announcement)
        self.assertNotIn("internal_notes", test_announcement)


@ddt
class MaintenanceAnnouncementDerivedFieldsTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.sp = self.fixture.service_provider
        self.client.force_authenticate(self.fixture.staff)
        self.ss = timezone.now().replace(microsecond=0)
        self.se = self.ss + timedelta(hours=2)

    @data((30, 30), (-30, -30), (0, 0))
    def test_overrun_minutes_returned_with_announcement(self, case):
        end_delta, expected = case
        announcement = marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.sp,
            state=MaintenanceState.COMPLETED,
            scheduled_start=self.ss,
            scheduled_end=self.se,
            actual_start=self.ss,
            actual_end=self.se + timedelta(minutes=end_delta),
        )
        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(announcement)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["overrun_minutes"], expected)

    def test_overrun_minutes_null_when_not_completed(self):
        announcement = marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.sp,
            state=MaintenanceState.SCHEDULED,
            scheduled_start=self.ss,
            scheduled_end=self.se,
        )
        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(announcement)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()["overrun_minutes"])

    def test_timing_bucket_returned_with_announcement(self):
        announcement = marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.sp,
            state=MaintenanceState.COMPLETED,
            scheduled_start=self.ss,
            scheduled_end=self.se,
            actual_start=self.ss,
            actual_end=self.se + timedelta(minutes=30),
        )
        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(announcement)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["timing_bucket"], "overrun")

    def _bucket(self, *, started=True, ended=True, start_delta=0, end_delta=0):
        announcement = models.MaintenanceAnnouncement(
            scheduled_start=self.ss,
            scheduled_end=self.se,
            actual_start=self.ss + timedelta(minutes=start_delta) if started else None,
            actual_end=self.se + timedelta(minutes=end_delta) if ended else None,
        )
        return announcement.timing_bucket

    def test_timing_bucket_classification(self):
        self.assertEqual(self._bucket(started=False, ended=False), "pending")
        self.assertEqual(self._bucket(start_delta=0, end_delta=0), "on_time")
        # 15-minute boundary is inclusive of on_time (tolerance is exceeded only above 15).
        self.assertEqual(self._bucket(start_delta=0, end_delta=15), "on_time")
        self.assertEqual(self._bucket(start_delta=0, end_delta=30), "overrun")
        # Start-side boundary: 15 min late is still on_time, 16 min is late_start.
        self.assertEqual(self._bucket(start_delta=15, end_delta=0), "on_time")
        self.assertEqual(self._bucket(start_delta=16, end_delta=0), "late_start")
        self.assertEqual(self._bucket(start_delta=30, end_delta=0), "late_start")
        self.assertEqual(self._bucket(start_delta=0, end_delta=-30), "early")


class MaintenanceStatsDerivedTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.maintenance_announcement.delete()
        self.sp = self.fixture.service_provider
        self.url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url(
            "maintenance_stats"
        )
        self.client.force_authenticate(self.fixture.staff)
        self.ss = timezone.now().replace(microsecond=0)
        self.se = self.ss + timedelta(hours=2)

    def _make(
        self, *, state, end_delta=None, maintenance_type=MaintenanceType.SCHEDULED
    ):
        started = state in (MaintenanceState.COMPLETED, MaintenanceState.IN_PROGRESS)
        return marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.sp,
            state=state,
            maintenance_type=maintenance_type,
            scheduled_start=self.ss,
            scheduled_end=self.se,
            actual_start=self.ss if started else None,
            actual_end=self.se + timedelta(minutes=end_delta)
            if end_delta is not None
            else None,
        )

    def test_maintenance_stats_on_time_rate_15min(self):
        self._make(state=MaintenanceState.COMPLETED, end_delta=0)  # within 15 min
        self._make(state=MaintenanceState.COMPLETED, end_delta=60)  # overran
        self._make(state=MaintenanceState.COMPLETED, end_delta=-30)  # early, within 15
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertAlmostEqual(
            response.data["summary"]["on_time_rate_15min"], 2 / 3, places=4
        )

    def test_maintenance_stats_avg_overrun_hours(self):
        self._make(state=MaintenanceState.COMPLETED, end_delta=0)  # not an overrun
        self._make(state=MaintenanceState.COMPLETED, end_delta=60)  # 1h overrun
        self._make(state=MaintenanceState.COMPLETED, end_delta=120)  # 2h overrun
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertAlmostEqual(
            response.data["summary"]["avg_overrun_hours"], 1.5, places=4
        )

    def test_maintenance_stats_emergency_count(self):
        self._make(
            state=MaintenanceState.SCHEDULED,
            maintenance_type=MaintenanceType.EMERGENCY,
        )
        self._make(
            state=MaintenanceState.SCHEDULED,
            maintenance_type=MaintenanceType.EMERGENCY,
        )
        self._make(
            state=MaintenanceState.SCHEDULED,
            maintenance_type=MaintenanceType.SCHEDULED,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["emergency_count"], 2)

    def test_maintenance_stats_tolerates_timing_ordering_and_bucket_params(self):
        # The stats endpoint must not 500 if the timing ordering/bucket params
        # (which rely on annotations only applied by those filters) leak in.
        self._make(state=MaintenanceState.COMPLETED, end_delta=60)
        response = self.client.get(
            self.url, {"o": "overrun_minutes", "timing_bucket": "overrun"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
