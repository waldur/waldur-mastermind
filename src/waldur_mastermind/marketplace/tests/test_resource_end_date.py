import datetime

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_mastermind.common.utils import parse_date
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class ResourceUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_url(self.resource)

    def make_request(self, user, payload=None):
        self.client.force_authenticate(user)
        payload = payload or {"name": "new_name", "description": "new description"}
        return self.client.patch(self.url, payload)

    def test_authorized_user_can_update_resource(self):
        response = self.make_request(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, "new_name")
        self.assertEqual(self.resource.description, "new description")

    def test_unauthorized_user_can_not_update_resource(self):
        response = self.make_request(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_authorized_user_can_update_end_date(self):
        with freeze_time("2020-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2021-01-01"})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertTrue(self.resource.end_date)
            self.assertEqual(self.resource.end_date_requested_by, self.fixture.staff)

    def test_authorized_user_can_set_current_past_date(self):
        with freeze_time("2020-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2020-01-01"})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertTrue(self.resource.end_date)

    def test_user_cannot_set_past_date(self):
        with freeze_time("2022-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2020-01-01"})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_end_date_should_generate_audit_log(self):
        with freeze_time("2020-01-01"):
            response = self.make_request(self.fixture.staff, {"end_date": "2021-01-01"})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertTrue(
                logging_models.Event.objects.filter(
                    message=f"End date of marketplace resource {self.resource.name} has been updated. End date: {self.resource.end_date}. User: {self.fixture.staff}."
                ).exists()
            )

    def test_resource_end_date_is_set_to_default_termination_if_required_and_not_provided(
        self,
    ):
        self.fixture.resource.offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
        }
        self.fixture.resource.offering.save()
        payload = {
            "name": "resource name update",
        }
        response = self.make_request(self.fixture.staff, payload)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["end_date"], self.fixture.resource.end_date)

    def test_end_date_is_not_updated_if_later_than_max_end_date(self):
        self.fixture.resource.offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        self.fixture.resource.offering.save()
        end_date = self.fixture.resource.created + datetime.timedelta(days=50)
        end_date = end_date.date()
        payload = {
            "end_date": end_date,
        }
        response = self.make_request(self.fixture.staff, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_date_is_updated_if_earlier_than_max_end_date(self):
        self.fixture.resource.offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        self.fixture.resource.offering.save()
        end_date = self.fixture.resource.created + datetime.timedelta(days=15)
        end_date = end_date.date()
        payload = {
            "end_date": end_date,
        }
        response = self.make_request(self.fixture.staff, payload)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["end_date"], self.fixture.resource.end_date, end_date
        )

    def test_end_date_is_not_updated_if_later_than_latest_date_for_resource_termination(
        self,
    ):
        with freeze_time("2022-01-01"):
            self.fixture.resource.offering.plugin_options = {
                "is_resource_termination_date_required": True,
                "default_resource_termination_offset_in_days": 7,
                "latest_date_for_resource_termination": "2030-01-01",
            }
            self.fixture.resource.offering.save()
            end_date = "2031-01-01"
            payload = {
                "end_date": end_date,
            }
            response = self.make_request(self.fixture.staff, payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_error_traceback(self):
        self.resource.state = ResourceStates.ERRED
        self.resource.error_traceback = "error_traceback"
        self.resource.save()
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.error_traceback)

    def test_changing_of_resource_should_generate_audit_log(self):
        response = self.make_request(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(
            logging_models.Event.objects.filter(
                event_type="marketplace_resource_update_succeeded",
                message__contains=self.resource.name,
            )
            .filter(message__contains="new_name")
            .count(),
            1,
        )

    def test_log_message_includes_name_of_relative_object(self):
        new_project = ProjectFactory()
        self.resource.project = new_project
        self.resource.save()
        self.assertEqual(
            logging_models.Event.objects.filter(
                event_type="marketplace_resource_update_succeeded",
                message__contains=self.resource.name,
            )
            .filter(message__contains=str(new_project))
            .count(),
            1,
        )


@ddt
class ResourceSetEndDateByProviderTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "set_end_date_by_provider"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_END_DATE)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_END_DATE)

    def make_request(self, user, payload):
        self.client.force_authenticate(user)
        return self.client.post(self.url, payload)

    @freeze_time("2020-01-01")
    def test_resource_is_not_used_for_last_3_months_and_end_date_is_7_days_in_future(
        self,
    ):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                self.fixture.offering_owner, {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.end_date, parse_date("2020-05-08"))

            self.assertTrue(
                logging_models.Event.objects.filter(
                    message__contains="End date of marketplace resource %s has been updated by provider."
                    % self.resource.name
                ).exists()
            )

    @freeze_time("2020-01-01")
    def test_resource_is_not_used_for_last_3_months_and_end_date_is_not_7_days_in_future(
        self,
    ):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                self.fixture.offering_owner, {"end_date": "2020-05-05"}
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2020-01-01")
    def test_resource_is_used_for_last_3_months_and_end_date_is_not_7_days_in_future(
        self,
    ):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        response = self.make_request(
            self.fixture.offering_owner, {"end_date": "2020-01-05"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2020-01-01")
    def test_resource_is_used_for_last_3_months_and_end_date_is_more_than_7_days_in_future(
        self,
    ):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        response = self.make_request(
            self.fixture.offering_owner, {"end_date": "2020-01-10"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff", "offering_owner", "service_manager", "global_support")
    @freeze_time("2020-01-01")
    def test_permission_positive(self, user):
        self.resource.state = ResourceStates.OK
        self.resource.save()

        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(
                self.resource.end_date_requested_by, getattr(self.fixture, user)
            )

    @data("admin", "manager", "member", "owner", "customer_support")
    @freeze_time("2020-01-01")
    def test_permission_negative(self, user):
        self.resource.state = ResourceStates.OK
        self.resource.save()

        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ResourceSetEndDateByStaffTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_url(
            self.resource, "set_end_date_by_staff"
        )

    def make_request(self, user, payload):
        self.client.force_authenticate(user)
        return self.client.post(self.url, payload)

    @freeze_time("2020-01-01")
    @data(
        "staff",
    )
    def test_user_can_set_end_date(self, user):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.end_date, parse_date("2020-05-08"))

            self.assertTrue(
                logging_models.Event.objects.filter(
                    message__contains="End date of marketplace resource %s has been updated by staff."
                    % self.resource.name
                ).exists()
            )
            self.resource.refresh_from_db()
            self.assertEqual(
                self.resource.end_date_requested_by, getattr(self.fixture, user)
            )

    @freeze_time("2020-01-01")
    @data("offering_owner", "service_manager")
    def test_user_cannot_set_end_date(self, user):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        with freeze_time("2020-05-01"):
            response = self.make_request(
                getattr(self.fixture, user), {"end_date": "2020-05-08"}
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@freeze_time("2024-01-01")
class ResourcePrepaidUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.staff = self.fixture.staff

        # Create a specific offering with a prepaid component
        self.prepaid_offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=self.prepaid_offering,
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
        )

        # Create a prepaid resource to test against
        # "now" is frozen at 2024-01-01, so 2025-01-01 is in the future.
        self.prepaid_resource = factories.ResourceFactory(
            offering=self.prepaid_offering,
            project=self.fixture.project,
            end_date=datetime.date(2025, 1, 1),
        )
        self.prepaid_url = factories.ResourceFactory.get_url(self.prepaid_resource)

        self.client.force_authenticate(self.staff)

    def test_cannot_change_end_date_for_prepaid_resource(self):
        # Arrange
        # The new date (2026-01-01) is after the frozen time (2024-01-01), so it's a valid future date.
        payload = {"end_date": "2026-01-01"}

        # Act
        response = self.client.patch(self.prepaid_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)
        self.assertIn(
            "Direct modification of the end date is not allowed for prepaid resources.",
            str(response.data["end_date"]),
        )

        # Verify the end_date was not changed in the database
        self.prepaid_resource.refresh_from_db()
        self.assertEqual(self.prepaid_resource.end_date, datetime.date(2025, 1, 1))

    def test_can_update_other_fields_for_prepaid_resource(self):
        # Arrange
        payload = {"name": "new prepaid name"}

        # Act
        response = self.client.patch(self.prepaid_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.prepaid_resource.refresh_from_db()
        self.assertEqual(self.prepaid_resource.name, "new prepaid name")

    def test_can_submit_same_end_date_for_prepaid_resource(self):
        # Arrange: The payload contains the same end_date that is already set.
        # This date (2025-01-01) is after the frozen time (2024-01-01), so the initial
        # date validation will now pass.
        payload = {"end_date": "2025-01-01"}

        # Act
        response = self.client.patch(self.prepaid_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.prepaid_resource.refresh_from_db()
        self.assertEqual(self.prepaid_resource.end_date, datetime.date(2025, 1, 1))
