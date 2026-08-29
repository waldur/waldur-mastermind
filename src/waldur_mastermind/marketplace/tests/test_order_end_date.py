import datetime

from constance.test.unittest import override_config as override_constance_config
from dateutil.relativedelta import relativedelta
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_mastermind.marketplace import models, serializers, signals
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
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

    @freeze_time("2026-07-27")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_end_date_allowed_when_within_max_offset_from_future_start_date(self):
        """
        Repro: project Sep 17–Dec 4, offering max_offset=120 from start_date.
        Dec 4 is beyond today+120 but within start_date+120 and project end.
        """
        self.project.start_date = datetime.date(2026, 9, 17)
        self.project.end_date = datetime.date(2026, 12, 4)
        self.project.save()

        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 90,
            "max_resource_termination_offset_in_days": 120,
        }
        offering.save()

        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={
                "start_date": "2026-09-17",
                "attributes": {
                    "name": "test",
                    "end_date": "2026-12-04",
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource = models.Resource.objects.last()
        self.assertEqual(resource.end_date, datetime.date(2026, 12, 4))

    @freeze_time("2026-07-27")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_end_date_rejected_when_beyond_max_offset_from_future_start_date(self):
        self.project.start_date = datetime.date(2026, 9, 17)
        self.project.end_date = datetime.date(2027, 6, 1)
        self.project.save()

        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 90,
            "max_resource_termination_offset_in_days": 120,
        }
        offering.save()

        # start_date + 120 = 2027-01-15; requested end is beyond that
        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={
                "start_date": "2026-09-17",
                "attributes": {
                    "name": "test",
                    "end_date": "2027-02-01",
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)

    @freeze_time("2026-07-27")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_end_date_rejected_when_after_project_end_date(self):
        self.project.start_date = datetime.date(2026, 9, 17)
        self.project.end_date = datetime.date(2026, 12, 4)
        self.project.save()

        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 90,
            "max_resource_termination_offset_in_days": 200,
        }
        offering.save()

        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={
                "start_date": "2026-09-17",
                "attributes": {
                    "name": "test",
                    "end_date": "2026-12-10",
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)

    @freeze_time("2026-07-27")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_end_date_rejected_when_before_start_date(self):
        self.project.start_date = datetime.date(2026, 9, 17)
        self.project.end_date = datetime.date(2026, 12, 4)
        self.project.save()

        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            "default_resource_termination_offset_in_days": 90,
            "max_resource_termination_offset_in_days": 120,
        }
        offering.save()

        # end_date is after today but before the order start_date
        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={
                "start_date": "2026-09-17",
                "attributes": {
                    "name": "test",
                    "end_date": "2026-08-01",
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.data)

    @freeze_time("2026-07-27")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_default_end_date_uses_future_start_date_and_caps_at_project_end(self):
        self.project.start_date = datetime.date(2026, 9, 17)
        self.project.end_date = datetime.date(2026, 12, 4)
        self.project.save()

        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering.plugin_options = {
            "is_resource_termination_date_required": True,
            # start_date + 90 = 2026-12-16, beyond project end → clamp to Dec 4
            "default_resource_termination_offset_in_days": 90,
            "max_resource_termination_offset_in_days": 120,
        }
        offering.save()

        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={"start_date": "2026-09-17"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource = models.Resource.objects.last()
        self.assertEqual(resource.end_date, datetime.date(2026, 12, 4))


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
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.CREATE_ORDER)

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

    @freeze_time("2024-01-01")
    def test_stated_months_outrank_the_date_derived_count(self):
        # The browser computed end_date from a local "today" one day ahead of
        # the server's: 12 months + 1 day would round up to 13 and fail a max
        # of 12, but the client stated 12 months and that is what is checked.
        self.prepaid_component.max_prepaid_duration = 12
        self.prepaid_component.save()
        payload = {
            "attributes": {"end_date": "2025-01-02", "prepaid_duration_months": 12}
        }

        response = self.create_order(self.user, self.offering, add_payload=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @freeze_time("2024-01-01")
    def test_without_stated_months_the_date_still_decides(self):
        self.prepaid_component.max_prepaid_duration = 12
        self.prepaid_component.save()
        payload = {"attributes": {"end_date": "2025-01-02"}}

        response = self.create_order(self.user, self.offering, add_payload=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)

    @freeze_time("2024-01-01")
    def test_stated_months_are_validated_against_the_component(self):
        payload = {
            "attributes": {"end_date": "2027-01-01", "prepaid_duration_months": 36}
        }

        response = self.create_order(self.user, self.offering, add_payload=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.prepaid_duration_months", response.data)

    @freeze_time("2024-01-01")
    def test_stated_months_must_be_a_positive_integer(self):
        payload = {
            "attributes": {"end_date": "2025-01-01", "prepaid_duration_months": "abc"}
        }

        response = self.create_order(self.user, self.offering, add_payload=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.prepaid_duration_months", response.data)

    def test_end_date_is_still_required_when_months_are_stated(self):
        payload = {"attributes": {"prepaid_duration_months": 12}}

        response = self.create_order(self.user, self.offering, add_payload=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)


class RenewalSerializerConstraintsTest(TestCase):
    """Tests for ResourceRenewSerializer renewal-specific duration constraints."""

    def setUp(self):
        self.offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        self.prepaid_component = factories.OfferingComponentFactory(
            offering=self.offering,
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
            min_renewal_duration=12,
            max_renewal_duration=60,
            renewal_duration_step=12,
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            end_date=timezone.now().date() + relativedelta(months=1),
        )

    def _validate(self, extension_months):
        serializer = serializers.ResourceRenewSerializer(
            data={"extension_months": extension_months},
            context={"resource": self.resource},
        )
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def test_valid_extension_equals_min(self):
        # 12 months: equals min, valid step (12-12)%12 == 0
        data = self._validate(12)
        self.assertEqual(data["extension_months"], 12)

    def test_valid_extension_multiple_of_step(self):
        # 24 months: (24-12)%12 == 0
        data = self._validate(24)
        self.assertEqual(data["extension_months"], 24)

    def test_invalid_extension_wrong_step(self):
        # 18 months: (18-12)%12 == 6 != 0
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            self._validate(18)
        self.assertIn("extension_months", ctx.exception.detail)

    def test_invalid_extension_below_min(self):
        # 6 months: below min of 12
        # Note: the serializer field has min_value=12 so this will fail at field level.
        # Set min_renewal_duration to something higher than 12 to test our validation.
        self.prepaid_component.min_renewal_duration = 24
        self.prepaid_component.max_renewal_duration = 60
        self.prepaid_component.renewal_duration_step = None
        self.prepaid_component.save()

        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            self._validate(12)
        self.assertIn("extension_months", ctx.exception.detail)
        self.assertIn("less than the minimum", str(ctx.exception.detail))

    def test_invalid_extension_above_max(self):
        # 72 months: above max of 60. The field allows up to 60 via max_value,
        # so set max_renewal_duration lower than the field max to test our validation.
        self.prepaid_component.min_renewal_duration = 12
        self.prepaid_component.max_renewal_duration = 36
        self.prepaid_component.renewal_duration_step = None
        self.prepaid_component.save()

        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            self._validate(48)
        self.assertIn("extension_months", ctx.exception.detail)
        self.assertIn("exceeds the maximum", str(ctx.exception.detail))

    def test_no_constraints_allows_any_positive_value(self):
        # Clear all renewal constraints
        self.prepaid_component.min_renewal_duration = None
        self.prepaid_component.max_renewal_duration = None
        self.prepaid_component.renewal_duration_step = None
        self.prepaid_component.save()

        # Any value within field bounds should be valid
        data = self._validate(36)
        self.assertEqual(data["extension_months"], 36)


class PrepaidDurationWithFutureStartDateTest(BaseOrderCreateTest):
    """Verify prepaid duration is calculated from order start_date, not today."""

    def setUp(self):
        super().setUp()
        self.offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=self.offering,
            is_prepaid=True,
            billing_type=models.BillingTypes.ONE_TIME,
            min_prepaid_duration=12,
            max_prepaid_duration=24,
            prepaid_duration_step=12,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)

    @freeze_time("2024-01-01")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_duration_calculated_from_start_date_not_today(self):
        # start_date=2024-03-01, end_date=2025-03-01 → 12 months (valid)
        # If calculated from today (2024-01-01), it would be 14 months (invalid with step=12)
        response = self.create_order(
            self.fixture.owner,
            self.offering,
            add_payload={
                "start_date": "2024-03-01",
                "attributes": {"end_date": "2025-03-01"},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @freeze_time("2024-01-01")
    @override_constance_config(ENABLE_ORDER_START_DATE=True)
    def test_duration_from_start_date_fails_if_too_short(self):
        # start_date=2024-03-01, end_date=2024-09-01 → 6 months (below min 12)
        response = self.create_order(
            self.fixture.owner,
            self.offering,
            add_payload={
                "start_date": "2024-03-01",
                "attributes": {"end_date": "2024-09-01"},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)
        self.assertIn(
            "less than the minimum required duration",
            response.data["attributes.end_date"][0],
        )


class CreateOrderUsesPrepaidConstraintsTest(BaseOrderCreateTest):
    """Confirm CREATE order validation uses prepaid (not renewal) fields."""

    @freeze_time("2024-01-01")
    def test_create_order_uses_min_prepaid_duration_not_renewal(self):
        # Arrange: min_prepaid_duration=3, min_renewal_duration=12
        # A 2-month order should fail due to min_prepaid_duration=3
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        factories.OfferingComponentFactory(
            offering=offering,
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
            min_prepaid_duration=3,
            max_prepaid_duration=24,
            min_renewal_duration=12,
            max_renewal_duration=60,
        )
        # 2024-03-01 is 2 months from 2024-01-01, less than min_prepaid_duration=3
        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={"attributes": {"end_date": "2024-03-01"}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes.end_date", response.data)
        self.assertIn(
            "less than the minimum required duration",
            response.data["attributes.end_date"][0],
        )


class ProcessorRenewalConstraintsTest(TestCase):
    """Tests for renewal duration constraint validation in the update processor."""

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=models.Offering.States.ACTIVE,
        )
        self.prepaid_component = factories.OfferingComponentFactory(
            offering=self.offering,
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
            min_renewal_duration=12,
            max_renewal_duration=60,
            renewal_duration_step=12,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.OK,
            end_date=timezone.now().date() + relativedelta(months=1),
        )

    def _make_renewal_order(self, extension_months):
        return factories.OrderFactory(
            offering=self.offering,
            plan=self.plan,
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
            attributes={"action": "renew", "extension_months": extension_months},
        )

    def test_processor_sends_failure_signal_for_invalid_step(self):
        # 18 months: (18-12)%12 != 0 — invalid step
        order = self._make_renewal_order(18)
        failure_signals = []

        def capture_signal(sender, order, error_message, **kwargs):
            failure_signals.append(error_message)

        signals.resource_limit_update_failed.connect(capture_signal)
        try:
            from waldur_mastermind.marketplace import processors

            processor = processors.BasicUpdateResourceProcessor(order)
            processor._process_renewal_or_limit_update(
                order.created_by, is_renewal=True
            )
        finally:
            signals.resource_limit_update_failed.disconnect(capture_signal)

        self.assertEqual(len(failure_signals), 1)
        self.assertIn("not a valid step", failure_signals[0])

    def test_processor_succeeds_for_valid_renewal(self):
        # 24 months: (24-12)%12 == 0 — valid
        order = self._make_renewal_order(24)
        failure_signals = []

        def capture_signal(sender, order, error_message, **kwargs):
            failure_signals.append(error_message)

        signals.resource_limit_update_failed.connect(capture_signal)
        try:
            from waldur_mastermind.marketplace import processors

            processor = processors.BasicUpdateResourceProcessor(order)
            processor._process_renewal_or_limit_update(
                order.created_by, is_renewal=True
            )
        finally:
            signals.resource_limit_update_failed.disconnect(capture_signal)

        self.assertEqual(len(failure_signals), 0)
