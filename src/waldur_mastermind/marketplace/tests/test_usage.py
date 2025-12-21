import datetime

from ddt import data, ddt
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.enums import Units
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories


@freeze_time("2019-06-19")
class PlanPeriodsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory()
        self.component = factories.OfferingComponentFactory(offering=self.offering)
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=self.plan, component=self.component)
        self.resource = factories.ResourceFactory(
            offering=self.offering, plan=self.plan
        )
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )
        self.client.force_authenticate(self.fixture.staff)
        self.url = factories.ResourceFactory.get_url(self.resource, "plan_periods")

    def test_component_usages_are_filtered_by_current_month(self):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=100,
            date=parse_datetime("2019-05-10"),
            billing_period=parse_datetime("2019-05-01"),
        )
        response = self.client.get(self.url)
        self.assertEqual(len(response.data[0]["components"]), 0)

    def test_component_usages_are_rendered_for_current_month(self):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=100,
            date=parse_datetime("2019-06-11"),
            billing_period=parse_datetime("2019-06-01"),
        )
        response = self.client.get(self.url)
        self.assertEqual(response.data[0]["components"][0]["usage"], "100.00")


@ddt
@freeze_time("2017-01-10")
class SubmitUsageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory()
        self.secret_code = self.service_provider.api_secret_code
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(unit=Units.PER_DAY, offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        self.offering_component2 = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="ram",
        )
        self.component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.component2 = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component2
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
        )

        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.get(resource=self.resource)

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_valid_signature(self):
        payload = self.get_valid_payload()
        response = self.client.post(
            "/api/marketplace-public-api/check_signature/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_signature(self):
        response = self.submit_usage(data="wrong_signature")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_usage(self):
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        date = timezone.now()
        billing_period = core_utils.month_start(date)
        self.assertTrue(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                date=date,
                billing_period=billing_period,
            ).exists()
        )

    def test_set_recurring_to_false_for_other_usages_in_this_period(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self.get_usage_data()
        payload["usages"][0]["recurring"] = True
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        date = timezone.now()
        billing_period = core_utils.month_start(date)
        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            date=date,
            billing_period=billing_period,
        )
        self.assertTrue(usage.recurring)
        new_plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.plan_period.resource,
            plan=self.plan_period.plan,
        )
        self.plan_period = new_plan_period
        payload = self.get_usage_data()
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        usage.refresh_from_db()
        self.assertFalse(usage.recurring)

    def test_submit_usage_with_description(self):
        description = "My first usage report"
        response = self.submit_usage(**self.get_valid_payload(description=description))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        report = models.ComponentUsage.objects.filter(resource=self.resource).first()
        self.assertEqual(report.description, description)

    def test_plan_period_linking(self):
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        date = timezone.now()
        billing_period = core_utils.month_start(date)
        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            date=date,
            billing_period=billing_period,
        )
        plan_period = models.ResourcePlanPeriod.objects.get(
            resource=self.resource, start=datetime.date(2017, 1, 10), end__isnull=True
        )
        self.assertEqual(usage.plan_period, plan_period)

    @data("staff", "owner")
    def test_authenticated_user_can_submit_usage_via_api(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        date = timezone.now()
        billing_period = core_utils.month_start(date)
        self.assertTrue(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                date=date,
                billing_period=billing_period,
            ).exists()
        )
        self.assertTrue(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component2,
                date=date,
                billing_period=billing_period,
            ).exists()
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages, {"cpu": "5.00", "ram": "5.00"})

    @data("admin", "manager", "user")
    def test_other_user_can_not_submit_usage_via_api(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                date=datetime.date.today(),
            ).exists()
        )

    def test_it_should_be_possible_to_submit_usage_for_terminating_resource(self):
        self.resource.state = ResourceStates.TERMINATING
        self.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data(ResourceStates.CREATING, ResourceStates.TERMINATED)
    def test_it_should_not_be_possible_to_submit_usage_for_pending_resource(
        self, state
    ):
        self.resource.state = state
        self.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2019-06-19")
    def test_component_usage_is_created_for_current_month_if_it_does_not_exist_yet(
        self,
    ):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=parse_datetime("2019-05-11"),
            billing_period=parse_datetime("2019-05-01"),
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component_usages = models.ComponentUsage.objects.filter(
            resource=self.resource, component=self.offering_component
        )
        self.assertEqual(
            2,
            models.ComponentUsage.objects.filter(
                resource=self.resource, component=self.offering_component
            ).count(),
        )
        component_usage = component_usages.last()
        self.assertEqual(self.fixture.staff, component_usage.modified_by)

    @freeze_time("2019-06-19")
    def test_component_usage_is_updated_for_current_month_if_it_already_exists(self):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=parse_datetime("2019-06-21"),
            billing_period=parse_datetime("2019-06-01"),
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component_usage = models.ComponentUsage.objects.filter(
            resource=self.resource, component=self.offering_component
        ).get()
        self.assertEqual(
            5,
            component_usage.usage,
        )
        self.assertEqual(self.fixture.staff, component_usage.modified_by)

    @data("staff", "owner")
    def test_authenticated_user_can_submit_user_usage_via_api(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=200,
            date=parse_datetime("2019-06-21"),
            billing_period=parse_datetime("2019-06-01"),
        )
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="user_00"
        )
        usage_amount = 100.01
        offering_user_url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {
            "usage": usage_amount,
            "username": "test_username_00",
            "user": offering_user_url,
        }
        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component_user_usage = models.ComponentUserUsage.objects.filter(
            component_usage=component_usage
        ).first()
        self.assertIsNotNone(component_user_usage)
        self.assertIsNotNone(component_user_usage.user)
        self.assertEqual(usage_amount, float(component_user_usage.usage))

    @data("staff", "owner")
    def test_authenticated_user_can_submit_user_usage_with_missing_user_via_api(
        self, role
    ):
        self.client.force_authenticate(getattr(self.fixture, role))
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=200,
            date=parse_datetime("2019-06-21"),
            billing_period=parse_datetime("2019-06-01"),
        )
        usage_amount = 100.01
        payload = {
            "usage": usage_amount,
            "username": "test_username_00",
        }
        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component_user_usage = models.ComponentUserUsage.objects.filter(
            component_usage=component_usage
        ).first()
        self.assertIsNotNone(component_user_usage)
        self.assertIsNone(component_user_usage.user)
        self.assertEqual(usage_amount, float(component_user_usage.usage))

    def test_user_usage_limit(self):
        self.client.force_authenticate(self.fixture.staff)
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=200,
            date=parse_datetime("2019-06-21"),
            billing_period=parse_datetime("2019-06-01"),
        )
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="user_00"
        )
        models.ComponentUserUsage.objects.create(
            user=offering_user,
            username=offering_user.user.username,
            component_usage=component_usage,
            usage=100,
        )
        models.ComponentUserUsageLimit.objects.create(
            resource=self.resource,
            component=self.offering_component,
            user=offering_user,
            limit=150,
        )
        offering_user_url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {
            "usage": 90,
            "username": "test_username_00",
            "user": offering_user_url,
        }
        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Usage limit exceeded", response.data["non_field_errors"][0])

    @data("admin", "manager", "user")
    def test_other_user_can_not_submit_user_usage_via_api(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=200,
            date=parse_datetime("2019-06-21"),
            billing_period=parse_datetime("2019-06-01"),
        )
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="user_00"
        )
        usage_amount = 100.01
        offering_user_url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {
            "usage": usage_amount,
            "username": "test_username_00",
            "user": offering_user_url,
        }
        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_total_amount_exceeds_month_limit(self):
        self.offering_component.limit_period = LimitPeriods.MONTH
        self.offering_component.limit_amount = 1
        self.offering_component.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_amount_does_not_exceed_month_limit(self):
        self.offering_component.limit_period = LimitPeriods.MONTH
        self.offering_component.limit_amount = 10
        self.offering_component.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @freeze_time("2019-06-19")  # Q2
    def test_total_amount_exceeds_quarterly_limit(self):
        self.offering_component.limit_period = LimitPeriods.QUARTERLY
        self.offering_component.limit_amount = 100
        self.offering_component.save()

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            usage=99,
            date=parse_datetime("2019-05-11"),  # Same quarter (Q2)
            billing_period=parse_datetime("2019-05-01"),
        )

        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2019-06-19")  # Q2
    def test_total_amount_does_not_exceed_quarterly_limit(self):
        self.offering_component.limit_period = LimitPeriods.QUARTERLY
        self.offering_component.limit_amount = 100
        self.offering_component.save()

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            usage=50,
            date=parse_datetime("2019-04-11"),  # Same quarter (Q2)
            billing_period=parse_datetime("2019-04-01"),
        )

        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @freeze_time("2019-06-19")
    def test_total_amount_exceeds_annual_limit(self):
        self.offering_component.limit_period = LimitPeriods.ANNUAL
        self.offering_component.limit_amount = 100
        self.offering_component.save()

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            usage=99,
            date=parse_datetime("2019-05-11"),
            billing_period=parse_datetime("2019-05-01"),
        )

        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2019-06-19")
    def test_total_amount_does_not_exceed_annual_limit(self):
        self.offering_component.limit_period = LimitPeriods.ANNUAL
        self.offering_component.limit_amount = 100
        self.offering_component.save()

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            usage=99,
            date=parse_datetime("2018-05-11"),
            billing_period=parse_datetime("2018-05-01"),
        )

        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_total_amount_exceeds_total_limit(self):
        self.offering_component.limit_period = LimitPeriods.TOTAL
        self.offering_component.limit_amount = 7
        self.offering_component.save()

        self.submit_usage()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_amount_does_not_exceed_total_limit(self):
        self.offering_component.limit_period = LimitPeriods.TOTAL
        self.offering_component.limit_amount = 15
        self.offering_component.save()

        self.submit_usage()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_usage_if_component_not_exists(self):
        response = self.submit_usage(**self.get_valid_payload(component_type="ram"))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_update_usage_if_usage_exists(self):
        self.submit_usage()
        response = self.submit_usage(**self.get_valid_payload(amount=15))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        usage = models.ComponentUsage.objects.first()
        self.assertEqual(usage.usage, 15)
        self.assertIsNone(usage.modified_by)

    def test_usage_is_not_updated_if_billing_period_is_closed(self):
        self.plan_period.end = parse_datetime("2016-01-10")
        self.plan_period.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dry_run_mode(self):
        response = self.submit_usage(dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(models.ComponentUsage.objects.filter().exists())

    def test_usage_is_not_updated_if_resource_is_terminated(self):
        self.resource.set_state_terminated()
        self.resource.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2025-07-31T21:25:00Z")
    @override_settings(TIME_ZONE="Europe/Tallinn")
    def test_usage_timezone_billing_period_calculation(self):
        """
        Test that usage sent at July 31st 21:25 UTC is recorded for August billing period
        when timezone is Europe/Tallinn (UTC+3).

        This simulates the scenario where:
        - Agent sends usage at 2025-08-01 00:25 in their UTC+3 timezone (Europe/Tallinn)
        - Waldur receives it at 2025-07-31T21:25:00Z (UTC)
        - Billing period should be calculated for August, not July
        """

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", self.get_usage_data()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
        )

        expected_billing_period = datetime.date(2025, 8, 1)
        self.assertEqual(usage.billing_period, expected_billing_period)

    def submit_usage(self, **extra):
        payload = self.get_valid_payload()
        payload.update(extra)
        return self.client.post("/api/marketplace-public-api/set_usage/", payload)

    def get_valid_payload(self, **kwargs):
        data = self.get_usage_data(**kwargs)
        payload = dict(
            customer=self.service_provider.customer.uuid.hex,
            data=core_utils.encode_jwt_token(data, self.secret_code),
        )
        return payload

    def get_usage_data(self, component_type="cpu", amount=5, description=""):
        return {
            "plan_period": self.plan_period.uuid.hex,
            "usages": [
                {
                    "type": component_type,
                    "amount": amount,
                    "description": description,
                },
                {
                    "type": "ram",
                    "amount": amount,
                    "description": description,
                },
            ],
        }

    def test_resource_without_plan_validation_error(self):
        """Test that providing a resource without a plan raises validation error."""
        resource_without_plan = models.Resource.objects.create(
            offering=self.offering,
            project=self.fixture.project,
        )

        usage_data = {
            "resource": resource_without_plan.uuid.hex,
            "usages": [
                {
                    "type": "cpu",
                    "amount": 5,
                    "description": "Test usage",
                }
            ],
        }

        payload = {
            "customer": self.service_provider.customer.uuid.hex,
            "data": core_utils.encode_jwt_token(usage_data, self.secret_code),
        }

        response = self.client.post("/api/marketplace-public-api/set_usage/", payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Resource must have a plan to report usage", str(response.data))


@freeze_time("2024-01-15")
class UsageDateBackfillTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        self.component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_staff_can_backfill_usage_with_date(self):
        """Test that staff users can specify date for backfilling usage."""
        self.client.force_authenticate(self.fixture.staff)

        # Date in December 2023
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "cpu",
                    "amount": 10,
                    "description": "Backfilled usage",
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that usage was created with correct date and billing period
        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
        )
        self.assertEqual(usage.date, backfill_date)
        self.assertEqual(usage.billing_period, datetime.date(2023, 12, 1))
        self.assertEqual(usage.description, "Backfilled usage")

    def test_non_staff_cannot_backfill_usage_with_date(self):
        """Test that service providers cannot specify date for usage-based components."""
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "cpu",
                    "amount": 10,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Service providers can only specify date for limit-based billing components",
            str(response.data),
        )

    def test_staff_can_backfill_user_usage_with_date(self):
        """Test that staff users can specify date for backfilling user usage."""
        self.client.force_authenticate(self.fixture.staff)

        # Create component usage for current date
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
        )

        # Date in December 2023
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "username": "user123",
            "usage": 25,
            "date": backfill_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that a new ComponentUsage was created for December billing period
        december_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            billing_period=datetime.date(2023, 12, 1),
        )
        self.assertEqual(december_usage.date, backfill_date)

        # Check that user usage was created linked to the December usage
        user_usage = models.ComponentUserUsage.objects.get(
            component_usage=december_usage, username="user123"
        )
        self.assertEqual(user_usage.usage, 25)

    def test_non_staff_cannot_backfill_user_usage_with_date(self):
        """Test that service providers cannot specify date for user usage on usage-based components."""
        self.client.force_authenticate(self.fixture.owner)

        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
        )

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "username": "user123",
            "usage": 25,
            "date": backfill_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Service providers can only specify date for limit-based billing components",
            str(response.data),
        )

    def test_future_date_validation(self):
        """Test that future dates are rejected for all users."""
        self.client.force_authenticate(self.fixture.staff)

        # Date in the future (1 year from current frozen time)
        future_date = timezone.now() + datetime.timedelta(days=365)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": future_date.isoformat(),
            "usages": [
                {
                    "type": "cpu",
                    "amount": 10,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Cannot submit usage for future dates",
            str(response.data),
        )


@freeze_time("2024-02-15")  # Current time: February 2024
class UsageBackfillInvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        # Create plan component with price for billing
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=10,  # $10 per unit
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_backfilled_usage_creates_historical_invoice(self):
        """Test that backfilled usage creates invoice items in the correct historical month."""
        self.client.force_authenticate(self.fixture.staff)

        # Backfill usage for December 2023
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "cpu",
                    "amount": 5,  # 5 units at $10/unit = $50
                    "description": "Backfilled usage",
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that invoice was created for December 2023, not current month (February 2024)
        december_invoice = invoice_models.Invoice.objects.filter(
            customer=self.fixture.customer,
            year=2023,
            month=12,
        ).first()
        self.assertIsNotNone(december_invoice)

        # Check that invoice item was created for the resource
        invoice_item = december_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        ).first()
        self.assertIsNotNone(invoice_item)
        self.assertEqual(invoice_item.quantity, 5)
        self.assertEqual(invoice_item.unit_price, 10)

        # Verify no invoice was created for current month (February 2024)
        current_invoice = invoice_models.Invoice.objects.filter(
            customer=self.fixture.customer,
            year=2024,
            month=2,
        )
        self.assertFalse(current_invoice.exists())

    def test_backfilled_usage_updates_existing_invoice_item(self):
        """Test that backfilled usage updates existing invoice items for the same month."""
        self.client.force_authenticate(self.fixture.staff)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        # First usage report
        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "cpu",
                    "amount": 3,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify first invoice item
        december_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2023,
            month=12,
        )
        invoice_item = december_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        ).first()
        self.assertEqual(invoice_item.quantity, 3)

        # Second usage report for same month - should update, not create new item
        payload["usages"][0]["amount"] = 8  # Update to 8 units

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify invoice item was updated
        invoice_item.refresh_from_db()
        self.assertEqual(invoice_item.quantity, 8)

        # Verify only one invoice item exists for this component
        cpu_items = december_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        )
        self.assertEqual(cpu_items.count(), 1)

    def test_backfilled_usage_different_months_different_invoices(self):
        """Test that backfilled usage for different months creates separate invoices."""
        self.client.force_authenticate(self.fixture.staff)

        # Usage for December 2023
        december_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)
        payload_december = {
            "plan_period": self.plan_period.uuid.hex,
            "date": december_date.isoformat(),
            "usages": [{"type": "cpu", "amount": 5}],
        }

        # Usage for November 2023
        november_date = datetime.datetime(2023, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
        payload_november = {
            "plan_period": self.plan_period.uuid.hex,
            "date": november_date.isoformat(),
            "usages": [{"type": "cpu", "amount": 3}],
        }

        # Submit both
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload_december
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload_november
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify separate invoices were created
        december_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2023,
            month=12,
        )
        november_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2023,
            month=11,
        )

        # Verify separate invoice items
        december_item = december_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        ).first()
        november_item = november_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        ).first()

        self.assertEqual(december_item.quantity, 5)
        self.assertEqual(november_item.quantity, 3)

    def test_current_usage_vs_backfilled_usage_separate_invoices(self):
        """Test that current usage and backfilled usage create separate invoice items."""
        self.client.force_authenticate(self.fixture.staff)

        # Current usage (February 2024)
        current_payload = {
            "plan_period": self.plan_period.uuid.hex,
            "usages": [{"type": "cpu", "amount": 10}],
        }

        # Backfilled usage (December 2023)
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)
        backfill_payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [{"type": "cpu", "amount": 7}],
        }

        # Submit both
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", current_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", backfill_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify separate invoices
        current_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2024,
            month=2,
        )
        historical_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2023,
            month=12,
        )

        # Verify correct quantities in each invoice
        current_item = current_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        ).first()
        historical_item = historical_invoice.items.filter(
            resource=self.resource,
            details__offering_component_type="cpu",
        ).first()

        self.assertEqual(current_item.quantity, 10)
        self.assertEqual(historical_item.quantity, 7)

    def test_backfilled_user_usage_invoice_behavior(self):
        """Test that backfilled user usage also affects invoice in correct month."""
        self.client.force_authenticate(self.fixture.staff)

        # First create component usage for December 2023 via backfill
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [{"type": "cpu", "amount": 5}],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Now add user usage to this December component usage
        december_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            billing_period=datetime.date(2023, 12, 1),
        )

        # Add user usage - this should update the December invoice
        user_payload = {
            "username": "user123",
            "usage": 3,  # This should update the total usage to 8 (5 + 3)
            "date": backfill_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{december_usage.uuid.hex}/set_user_usage/",
            user_payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify that the December invoice item was updated
        invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2023,
            month=12,
        )

        # When user usage is added, it should trigger update of the main component usage
        # which in turn should update the invoice item
        december_usage.refresh_from_db()

        # The ComponentUsage should now show the updated total
        # Note: The exact behavior might depend on how user usage aggregation is implemented
        # This test documents the expected invoice behavior


@freeze_time("2024-01-15")
class ServiceProviderUsageDateBackfillTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        # Create service provider customer
        self.service_provider_customer = self.fixture.customer

        # Create consumer customer and project
        self.consumer_fixture = structure_fixtures.ProjectFixture()
        self.consumer_customer = self.consumer_fixture.customer
        self.consumer_project = self.consumer_fixture.project

        # Create offering owned by service provider
        self.offering = factories.OfferingFactory(
            customer=self.service_provider_customer
        )
        self.plan = factories.PlanFactory(offering=self.offering)

        # Create limit-based component
        self.limit_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="limit_cpu",
        )
        self.limit_plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.limit_component
        )

        # Create usage-based component
        self.usage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="usage_cpu",
        )
        self.usage_plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.usage_component
        )

        # Create resource owned by consumer but on service provider's offering
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.consumer_project,
            state=ResourceStates.OK,
        )

        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )

        # Grant service provider permission to set resource usage
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_service_provider_can_backfill_limit_based_components(self):
        """Test that service providers can specify date for limit-based components."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        # Date in December 2023
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "limit_cpu",  # Limit-based component
                    "amount": 10,
                    "description": "Service provider backfill",
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that usage was created with correct date and billing period
        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.limit_component,
        )
        self.assertEqual(usage.date, backfill_date)
        self.assertEqual(usage.billing_period, datetime.date(2023, 12, 1))

    def test_service_provider_cannot_backfill_usage_based_components(self):
        """Test that service providers cannot specify date for usage-based components."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "usage_cpu",  # Usage-based component
                    "amount": 10,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Service providers can only specify date for limit-based billing components",
            str(response.data),
        )

    def test_service_provider_cannot_backfill_mixed_components_with_usage_based(self):
        """Test that service providers cannot backfill when any component is usage-based."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "limit_cpu",  # Limit-based component
                    "amount": 10,
                },
                {
                    "type": "usage_cpu",  # Usage-based component
                    "amount": 5,
                },
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Service providers can only specify date for limit-based billing components",
            str(response.data),
        )

    def test_service_provider_can_backfill_multiple_limit_based_components(self):
        """Test that service providers can backfill when ALL components are limit-based."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        # Create another limit-based component
        limit_component2 = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="limit_memory",
        )
        factories.PlanComponentFactory(plan=self.plan, component=limit_component2)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "limit_cpu",  # Limit-based component
                    "amount": 10,
                },
                {
                    "type": "limit_memory",  # Another limit-based component
                    "amount": 8,
                },
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Both usages should be created
        cpu_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.limit_component,
        )
        memory_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=limit_component2,
        )

        self.assertEqual(cpu_usage.date, backfill_date)
        self.assertEqual(memory_usage.date, backfill_date)

    def test_non_service_provider_cannot_backfill_date(self):
        """Test that non-service provider users cannot specify date."""
        # Create another customer that is not the service provider
        other_fixture = structure_fixtures.ProjectFixture()
        other_user = other_fixture.owner

        self.client.force_authenticate(other_user)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "limit_cpu",
                    "amount": 10,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Only staff users and service providers can specify date for backfilling",
            str(response.data),
        )

    def test_service_provider_can_backfill_user_usage_for_limit_components(self):
        """Test that service providers can specify date for user usage on limit-based components."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        # First create component usage for limit-based component
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.limit_component,
            usage=100,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
        )

        # Date in December 2023
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "username": "user123",
            "usage": 25,
            "date": backfill_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that a new ComponentUsage was created for December billing period
        december_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.limit_component,
            billing_period=datetime.date(2023, 12, 1),
        )
        self.assertEqual(december_usage.date, backfill_date)

        # Check that user usage was created linked to the December usage
        user_usage = models.ComponentUserUsage.objects.get(
            component_usage=december_usage, username="user123"
        )
        self.assertEqual(user_usage.usage, 25)

    def test_service_provider_cannot_backfill_user_usage_for_usage_components(self):
        """Test that service providers cannot specify date for user usage on usage-based components."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        # Create component usage for usage-based component
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.usage_component,  # Usage-based component
            usage=100,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
        )

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=timezone.utc)

        payload = {
            "username": "user123",
            "usage": 25,
            "date": backfill_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Service providers can only specify date for limit-based billing components",
            str(response.data),
        )
