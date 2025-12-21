import datetime

from freezegun import freeze_time
from rest_framework import status

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest


class OrderEndDateCreateTest(BaseOrderCreateTest):
    @freeze_time("2024-01-01")
    def test_set_end_date(self):
        user = self.fixture.staff
        response = self.create_order(
            user, add_payload={"attributes": {"end_date": "2025-01-01"}}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        resource = order.resource
        self.assertTrue(resource.end_date)
        self.assertEqual(resource.end_date_requested_by, user)

    def test_resource_end_date_set_to_default_if_required_but_not_provided(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
        }
        offering.save()

        response = self.create_order(self.fixture.owner, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        resource = models.Resource.objects.last()
        end_date = resource.created + datetime.timedelta(days=7)
        self.assertEqual(resource.end_date, end_date.date())

    def test_missing_default_offset_configuration(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            # Missing default_resource_termination_offset_in_days
        }
        offering.save()

        response = self.create_order(self.fixture.owner, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)

    @freeze_time("2022-01-01")
    def test_resource_is_not_created_if_end_date_later_than_max_end_date(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        offering.save()
        end_date = datetime.date(2025, 12, 25)

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_is_created_if_end_date_earlier_than_max_end_date(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)

        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "max_resource_termination_offset_in_days": 30,
        }
        offering.save()
        end_date = datetime.date.today() + datetime.timedelta(days=10)

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource = models.Resource.objects.last()
        self.assertEqual(resource.end_date, end_date)

    @freeze_time("2022-01-01")
    def test_resource_is_not_created_if_end_date_later_than_latest_date_for_resource_termination(
        self,
    ):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 7,
            "latest_date_for_resource_termination": "2030-01-01",
        }
        offering.save()
        end_date = datetime.date(2031, 12, 25)

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2022-01-01")
    def test_default_date_truncated_by_global_limit(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 3650,  # 10 years
            "latest_date_for_resource_termination": "2025-01-01",
        }
        offering.save()

        response = self.create_order(self.fixture.owner, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        resource = models.Resource.objects.last()
        self.assertEqual(resource.end_date, datetime.date(2025, 1, 1))

    def test_malformed_date_string(self):
        response = self.create_order(
            self.fixture.staff,
            add_payload={"attributes": {"end_date": "2025/01/01"}},  # Wrong format
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)

    @freeze_time("2022-01-01")
    def test_end_date_before_creation_date(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
        }
        offering.save()
        end_date = datetime.date(2021, 12, 31)  # Before creation date

        response = self.create_order(
            self.fixture.owner,
            offering,
            {"attributes": {"name": "test", "end_date": end_date}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrderCreatePrepaidTest(BaseOrderCreateTest):
    def setUp(self):
        super().setUp()
        self.offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        self.prepaid_component = factories.OfferingComponentFactory(
            offering=self.offering,
            is_prepaid=True,
            billing_type=models.BillingTypes.ONE_TIME,
            min_prepaid_duration=3,  # months
            max_prepaid_duration=24,  # months
        )
        self.user = self.fixture.owner

    @freeze_time("2024-01-01")
    def test_create_prepaid_order_succeeds_with_valid_end_date(self):
        # Arrange: 12 months duration, which is between min (3) and max (24)
        valid_end_date = "2025-01-01"
        payload = {"attributes": {"end_date": valid_end_date}}

        # Act
        response = self.create_order(self.user, self.offering, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.end_date.isoformat(), valid_end_date)
        self.assertEqual(order.resource.end_date_requested_by, self.user)

    def test_create_prepaid_order_fails_without_end_date(self):
        # Arrange: Payload is missing the end_date attribute
        payload = {"attributes": {"name": "Test resource without end date"}}

        # Act
        response = self.create_order(self.user, self.offering, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)
        self.assertIn(
            "required for prepaid offerings", response.data["attributes.end_date"][0]
        )

    @freeze_time("2024-01-01")
    def test_create_prepaid_order_fails_if_duration_is_too_short(self):
        # Arrange: 2 months duration, which is less than the required min (3)
        short_end_date = "2024-03-01"
        payload = {"attributes": {"end_date": short_end_date}}

        # Act
        response = self.create_order(self.user, self.offering, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)
        self.assertIn(
            "less than the minimum required duration",
            response.data["attributes.end_date"][0],
        )

    @freeze_time("2024-01-01")
    def test_create_prepaid_order_fails_if_duration_is_too_long(self):
        # Arrange: 36 months duration, which is more than the allowed max (24)
        long_end_date = "2027-01-01"
        payload = {"attributes": {"end_date": long_end_date}}

        # Act
        response = self.create_order(self.user, self.offering, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)
        self.assertIn(
            "exceeds the maximum allowed duration",
            response.data["attributes.end_date"][0],
        )

    @freeze_time("2024-01-15")
    def test_create_prepaid_order_fails_if_duration_is_just_under_minimum(self):
        # Arrange: min duration is 3 months.
        # End date of 2024-03-14 gives relativedelta(months=1, days=28).
        # The serializer calculates this as 1 + 1 = 2 months.
        # Since 2 < 3, this request should FAIL.
        short_end_date = "2024-03-14"
        payload = {"attributes": {"end_date": short_end_date}}

        # Act
        response = self.create_order(self.user, self.offering, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "less than the minimum required duration",
            response.data["attributes.end_date"][0],
        )

    @freeze_time("2024-01-15")
    def test_create_prepaid_order_succeeds_if_duration_is_at_minimum_due_to_rounding(
        self,
    ):
        # Arrange: min duration is 3 months.
        # End date of 2024-03-15 gives relativedelta(months=2, days=0).
        # This is exactly 2 months, which is less than 3. This should FAIL.
        # Let's try 2024-04-14 -> relativedelta(months=2, days=30) -> 3 months. This should PASS.
        # Let's try 2024-03-16 -> relativedelta(months=2, days=1) -> 3 months. This should PASS.
        valid_end_date = "2024-03-16"
        payload = {"attributes": {"end_date": valid_end_date}}

        # Act
        response = self.create_order(self.user, self.offering, add_payload=payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
