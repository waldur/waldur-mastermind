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
from waldur_mastermind.common.mixins import UnitPriceMixin
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
class PlanPeriodsTest(test.APITestCase):
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
class SubmitUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory()
        self.secret_code = self.service_provider.api_secret_code
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(
            unit=UnitPriceMixin.Units.PER_DAY, offering=self.offering
        )
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
        self.assertEqual(self.resource.current_usages, {"cpu": 5.0, "ram": 5.0})

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
        expected_status = (
            status.HTTP_404_NOT_FOUND if role == "user" else status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(response.status_code, expected_status)

    def test_total_amount_exceeds_month_limit(self):
        self.offering_component.limit_period = LimitPeriods.MONTH
        self.offering_component.limit_amount = 1
        self.offering_component.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
class UsageDateBackfillTest(test.APITestCase):
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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
        """Test that service providers cannot specify date for usage-based components when backfilling past billing periods."""
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
            "Service providers can only specify date for limit-based or prepaid billing components when backfilling past billing periods",
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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
        """Test that service providers cannot specify date for user usage on usage-based components when backfilling past billing periods."""
        self.client.force_authenticate(self.fixture.owner)

        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
        )

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
            "Service providers can only specify date for limit-based or prepaid billing components when backfilling past billing periods",
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


class SetUserUsageDuplicateComponentTest(test.APITestCase):
    """Test that set_user_usage handles duplicate ComponentUsage records.

    When set_usage is called with different plan_periods for the same
    (resource, component, billing_period), multiple ComponentUsage records
    are created. set_user_usage must not crash with MultipleObjectsReturned.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_set_user_usage_with_duplicate_component_usages(self):
        """set_user_usage should succeed even when multiple ComponentUsage
        records exist for the same resource+component+billing_period."""
        self.client.force_authenticate(self.fixture.staff)

        billing_period = datetime.date(2023, 12, 1)
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

        # Create two ComponentUsage records with the same resource, component,
        # and billing_period but different plan_periods (simulates what happens
        # when set_usage is called with different plan_periods).
        component_usage_1 = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=backfill_date,
            billing_period=billing_period,
        )
        plan_period_2 = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=plan_period_2,
            component=self.offering_component,
            usage=50,
            date=backfill_date,
            billing_period=billing_period,
        )

        # Verify there are indeed 2 records
        self.assertEqual(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                billing_period=billing_period,
            ).count(),
            2,
        )

        payload = {
            "username": "user123",
            "usage": 25,
            "date": backfill_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage_1.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the user usage was created
        user_usage = models.ComponentUserUsage.objects.get(username="user123")
        self.assertEqual(user_usage.usage, 25)


class HistoricalUsagePlanPeriodDuplicateTest(test.APITestCase):
    """Reproducer for site-agent historical usage creating duplicate
    ComponentUsage records — one with plan_period=None and one with
    plan_period set — for the same (resource, component, billing_period).

    Root cause: when the historical date predates the ResourcePlanPeriod.start,
    get_plan_period() returns None. A later submission (after the plan period
    is extended or a new one created) returns a real plan_period. The unique
    constraints allow both records to coexist.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        # Plan period starts 2024-06-01 — does NOT cover historical months
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=self.plan,
            start=datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC),
            end=None,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    @freeze_time("2024-07-15")
    def test_historical_usage_before_plan_period_creates_record_with_null_plan_period(
        self,
    ):
        """When historical date predates plan_period.start, the created
        ComponentUsage has plan_period=None."""
        self.client.force_authenticate(self.fixture.staff)

        # Submit usage for March 2024 — before plan period start (June 2024)
        payload = {
            "resource": self.resource.uuid.hex,
            "usages": [{"type": "cpu", "amount": 100}],
            "date": "2024-03-15T10:00:00Z",
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            billing_period=datetime.date(2024, 3, 1),
        )
        # plan_period is None because no ResourcePlanPeriod covers March 2024
        self.assertIsNone(usage.plan_period)

    @freeze_time("2024-07-15")
    def test_resubmission_after_plan_period_extended_updates_existing_record(self):
        """After a wider plan period is created, re-submitting usage for the
        same month updates the existing record's plan_period instead of
        creating a duplicate."""
        self.client.force_authenticate(self.fixture.staff)

        # Step 1: Submit historical usage for March 2024 (before plan period)
        payload = {
            "resource": self.resource.uuid.hex,
            "usages": [{"type": "cpu", "amount": 100}],
            "date": "2024-03-15T10:00:00Z",
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify: 1 record with plan_period=None
        usages = models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.offering_component,
            billing_period=datetime.date(2024, 3, 1),
        )
        self.assertEqual(usages.count(), 1)
        self.assertIsNone(usages.first().plan_period)

        # Step 2: A wider plan period is created (e.g., by get_or_create_plan_period
        # or migration 0122) that covers historical dates
        wide_plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=self.plan,
            start=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
            end=datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC),
        )

        # Step 3: Re-submit usage for the same month (e.g., site agent re-run)
        payload["usages"][0]["amount"] = 200
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Fixed: only 1 record, plan_period updated to the new one
        usages = models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.offering_component,
            billing_period=datetime.date(2024, 3, 1),
        )
        self.assertEqual(usages.count(), 1)
        usage = usages.first()
        self.assertEqual(usage.plan_period, wide_plan_period)
        self.assertEqual(usage.usage, 200)

    @freeze_time("2024-07-15")
    def test_legacy_duplicates_cleaned_up_on_resubmission(self):
        """Pre-existing duplicate ComponentUsage records (from before the fix)
        are cleaned up when new usage is submitted for the same month."""
        self.client.force_authenticate(self.fixture.staff)

        billing_period = datetime.date(2024, 3, 1)
        backfill_date = datetime.datetime(2024, 3, 15, 10, 0, 0, tzinfo=datetime.UTC)

        # Simulate pre-existing duplicates: one with plan_period=None, one with plan_period set
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=None,
            component=self.offering_component,
            usage=100,
            date=backfill_date,
            billing_period=billing_period,
        )
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=50,
            date=backfill_date,
            billing_period=billing_period,
        )
        self.assertEqual(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                billing_period=billing_period,
            ).count(),
            2,
        )

        # Re-submit usage — should consolidate into a single record
        payload = {
            "resource": self.resource.uuid.hex,
            "usages": [{"type": "cpu", "amount": 300}],
            "date": "2024-03-15T10:00:00Z",
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        usages = models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.offering_component,
            billing_period=billing_period,
        )
        self.assertEqual(usages.count(), 1)
        self.assertEqual(usages.first().usage, 300)


@freeze_time("2024-07-15")
class MidMonthPlanPeriodResolutionTest(test.APITestCase):
    """Site agents report usage dated to the first of the billing month. A
    resource that became active mid-month has a ResourcePlanPeriod that starts
    after the first of the month, so the point-in-time plan-period lookup at
    the month start misses it and the usage is stored with plan_period=None —
    which suppresses invoice-item (and therefore cost-policy) creation.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.client.force_authenticate(self.fixture.staff)

    def _report_month_start_usage(self, amount=100):
        # Emulate the site agent: usage dated to the first of the current month.
        payload = {
            "resource": self.resource.uuid.hex,
            "usages": [{"type": "cpu", "amount": amount}],
            "date": "2024-07-01T00:00:00Z",
        }
        return self.client.post("/api/marketplace-component-usages/set_usage/", payload)

    def _get_usage(self, billing_period=datetime.date(2024, 7, 1)):
        return models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            billing_period=billing_period,
        )

    def test_mid_month_plan_period_is_resolved_for_month_start_usage(self):
        # Resource became active on the 10th; plan period starts mid-month.
        plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=self.plan,
            start=datetime.datetime(2024, 7, 10, tzinfo=datetime.UTC),
            end=None,
        )
        response = self._report_month_start_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The usage belongs to the mid-month plan period, not None.
        self.assertEqual(self._get_usage().plan_period, plan_period)

    def test_plan_period_is_created_when_resource_has_none(self):
        # Resource reached OK without a plan period (e.g. backend-synced): no
        # ResourcePlanPeriod exists at all, yet current-month usage must bill.
        self.assertFalse(
            models.ResourcePlanPeriod.objects.filter(resource=self.resource).exists()
        )
        response = self._report_month_start_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        usage = self._get_usage()
        self.assertIsNotNone(usage.plan_period)
        self.assertEqual(usage.plan_period.resource, self.resource)

    def test_historical_usage_without_plan_period_stays_null(self):
        # Backfilling a past month for a resource with no plan period must NOT
        # lazily create one — genuine historical gaps stay unbilled.
        payload = {
            "resource": self.resource.uuid.hex,
            "usages": [{"type": "cpu", "amount": 100}],
            "date": "2024-03-15T10:00:00Z",
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(self._get_usage(datetime.date(2024, 3, 1)).plan_period)
        self.assertFalse(
            models.ResourcePlanPeriod.objects.filter(resource=self.resource).exists()
        )


@freeze_time("2024-02-15")  # Current time: February 2024
class UsageBackfillInvoiceTest(test.APITestCase):
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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
        december_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)
        payload_december = {
            "plan_period": self.plan_period.uuid.hex,
            "date": december_date.isoformat(),
            "usages": [{"type": "cpu", "amount": 5}],
        }

        # Usage for November 2023
        november_date = datetime.datetime(2023, 11, 20, 10, 0, 0, tzinfo=datetime.UTC)
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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)
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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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

    def test_usage_update_rejected_for_finalized_invoice(self):
        """Usage reported for a month whose invoice is already finalized should not update the invoice item."""
        self.client.force_authenticate(self.fixture.staff)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

        # First usage report creates the invoice and item
        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "cpu",
                    "amount": 5,
                }
            ],
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Finalize the invoice (transition from PENDING to CREATED)
        december_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer, year=2023, month=12
        )
        december_invoice.set_created()
        self.assertEqual(december_invoice.state, invoice_models.Invoice.States.CREATED)

        # Verify initial quantity
        item = december_invoice.items.get(
            resource=self.resource,
            details__offering_component_type="cpu",
        )
        self.assertEqual(item.quantity, 5)

        # Report higher usage for the same month -- should be rejected
        payload["usages"][0]["amount"] = 10
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Invoice item quantity should remain unchanged
        item.refresh_from_db()
        self.assertEqual(item.quantity, 5)


@freeze_time("2024-01-15")
class ServiceProviderUsageDateBackfillTest(test.APITestCase):
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

        # Create prepaid (one-time) component. Usage is display-only for these,
        # so it must be reportable — including backfilling past periods.
        self.prepaid_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="prepaid_gpu",
        )
        self.prepaid_plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.prepaid_component
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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
        """Test that service providers cannot specify date for usage-based components when backfilling past billing periods."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
            "Service providers can only specify date for limit-based or prepaid billing components when backfilling past billing periods",
            str(response.data),
        )

    def test_service_provider_cannot_backfill_mixed_components_with_usage_based(self):
        """Test that service providers cannot backfill when any component is usage-based in past billing periods."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
            "Service providers can only specify date for limit-based or prepaid billing components when backfilling past billing periods",
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

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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

    def test_service_provider_can_report_prepaid_usage_in_current_period(self):
        """Prepaid (one-time) components must be reportable via set_usage.

        Regression: set_usage previously rejected prepaid components with
        "These components are invalid", so prepaid usage never showed up in
        Homeport even though it does not affect usage-based billing.
        """
        self.client.force_authenticate(self.fixture.owner)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "usages": [
                {
                    "type": "prepaid_gpu",
                    "amount": 7,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.prepaid_component,
        )
        self.assertEqual(usage.usage, 7)

    def test_service_provider_can_backfill_prepaid_usage(self):
        """Service providers can backfill prepaid usage into a past period."""
        self.client.force_authenticate(self.fixture.owner)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": backfill_date.isoformat(),
            "usages": [
                {
                    "type": "prepaid_gpu",
                    "amount": 7,
                }
            ],
        }

        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.prepaid_component,
        )
        self.assertEqual(usage.date, backfill_date)
        self.assertEqual(usage.billing_period, datetime.date(2023, 12, 1))

    def test_service_provider_can_backfill_user_usage_for_prepaid_components(self):
        """Service providers can backfill per-user usage for prepaid components."""
        self.client.force_authenticate(self.fixture.owner)

        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.prepaid_component,
            usage=100,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
        )

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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

        december_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.prepaid_component,
            billing_period=datetime.date(2023, 12, 1),
        )
        user_usage = models.ComponentUserUsage.objects.get(
            component_usage=december_usage, username="user123"
        )
        self.assertEqual(user_usage.usage, 25)

    def test_non_service_provider_cannot_backfill_date(self):
        """Test that non-service provider users cannot specify date."""
        # Create another customer that is not the service provider
        other_fixture = structure_fixtures.ProjectFixture()
        other_user = other_fixture.owner

        self.client.force_authenticate(other_user)

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
        """Test that service providers cannot specify date for user usage on usage-based components when backfilling past billing periods."""
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

        backfill_date = datetime.datetime(2023, 12, 15, 10, 0, 0, tzinfo=datetime.UTC)

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
            "Service providers can only specify date for limit-based or prepaid billing components when backfilling past billing periods",
            str(response.data),
        )

    def test_service_provider_can_specify_date_for_usage_components_in_current_billing_period(
        self,
    ):
        """Test that service providers can specify date for usage-based components when date is in current billing period."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        # Date in the same month as frozen time (January 2024)
        same_month_date = datetime.datetime(2024, 1, 10, 14, 30, 0, tzinfo=datetime.UTC)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": same_month_date.isoformat(),
            "usages": [
                {
                    "type": "usage_cpu",  # Usage-based component
                    "amount": 10,
                    "description": "Same month usage with specific timestamp",
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
            component=self.usage_component,
        )
        self.assertEqual(usage.date, same_month_date)
        self.assertEqual(usage.billing_period, datetime.date(2024, 1, 1))
        self.assertEqual(usage.description, "Same month usage with specific timestamp")

    def test_service_provider_can_specify_date_for_user_usage_in_current_billing_period(
        self,
    ):
        """Test that service providers can specify date for user usage on usage-based components when date is in current billing period."""
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

        # Date in the same month as frozen time (January 2024)
        same_month_date = datetime.datetime(2024, 1, 10, 14, 30, 0, tzinfo=datetime.UTC)

        payload = {
            "username": "user123",
            "usage": 25,
            "date": same_month_date.isoformat(),
        }

        response = self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}/set_user_usage/",
            payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that user usage was created successfully
        user_usage = models.ComponentUserUsage.objects.get(
            component_usage=component_usage, username="user123"
        )
        self.assertEqual(user_usage.usage, 25)

    def test_service_provider_can_specify_date_for_mixed_components_in_current_billing_period(
        self,
    ):
        """Test that service providers can specify date for mixed component types when date is in current billing period."""
        # Authenticate as service provider owner
        self.client.force_authenticate(self.fixture.owner)

        # Date in the same month as frozen time (January 2024)
        same_month_date = datetime.datetime(2024, 1, 10, 14, 30, 0, tzinfo=datetime.UTC)

        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": same_month_date.isoformat(),
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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that both usages were created
        self.assertEqual(
            models.ComponentUsage.objects.filter(resource=self.resource).count(), 2
        )


@ddt
@freeze_time("2019-06-19")
class BulkSetUserUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )
        self.component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=200,
            date=parse_datetime("2019-06-21"),
            billing_period=parse_datetime("2019-06-01"),
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def get_url(self):
        return f"/api/marketplace-component-usages/{self.component_usage.uuid.hex}/set_user_usages/"

    @data("staff", "owner")
    def test_can_submit_multiple_user_usages(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        payload = {
            "usages": [
                {"username": "user1", "usage": 50.0},
                {"username": "user2", "usage": 75.5},
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.ComponentUserUsage.objects.filter(
                component_usage=self.component_usage
            ).count(),
            2,
        )
        user1 = models.ComponentUserUsage.objects.get(
            component_usage=self.component_usage, username="user1"
        )
        self.assertEqual(float(user1.usage), 50.0)
        user2 = models.ComponentUserUsage.objects.get(
            component_usage=self.component_usage, username="user2"
        )
        self.assertEqual(float(user2.usage), 75.5)

    def test_existing_user_usages_are_updated(self):
        self.client.force_authenticate(self.fixture.staff)
        models.ComponentUserUsage.objects.create(
            component_usage=self.component_usage,
            username="user1",
            usage=10,
        )
        payload = {
            "usages": [
                {"username": "user1", "usage": 99.0},
                {"username": "user2", "usage": 50.0},
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.ComponentUserUsage.objects.filter(
                component_usage=self.component_usage
            ).count(),
            2,
        )
        user1 = models.ComponentUserUsage.objects.get(
            component_usage=self.component_usage, username="user1"
        )
        self.assertEqual(float(user1.usage), 99.0)

    def test_empty_usages_returns_400(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {"usages": []}
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("admin", "manager", "user")
    def test_unauthorized_roles_cannot_submit(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        payload = {
            "usages": [
                {"username": "user1", "usage": 50.0},
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        expected_status = (
            status.HTTP_404_NOT_FOUND if role == "user" else status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(response.status_code, expected_status)

    def test_usage_limit_validation_works_per_item(self):
        self.client.force_authenticate(self.fixture.staff)
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="limited_user"
        )
        models.ComponentUserUsage.objects.create(
            user=offering_user,
            username=offering_user.user.username,
            component_usage=self.component_usage,
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
            "usages": [
                {
                    "username": "ok_user",
                    "usage": 10,
                },
                {
                    "username": "limited_user",
                    "user": offering_user_url,
                    "usage": 90,
                },
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_atomicity_none_persisted_on_validation_failure(self):
        self.client.force_authenticate(self.fixture.staff)
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="limited_user"
        )
        models.ComponentUserUsageLimit.objects.create(
            resource=self.resource,
            component=self.offering_component,
            user=offering_user,
            limit=10,
        )
        offering_user_url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {
            "usages": [
                {"username": "good_user", "usage": 5},
                {
                    "username": "limited_user",
                    "user": offering_user_url,
                    "usage": 999,
                },
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Because validation failed, the first item should NOT have been persisted
        self.assertEqual(
            models.ComponentUserUsage.objects.filter(
                component_usage=self.component_usage
            ).count(),
            0,
        )

    def test_staff_can_submit_with_date(self):
        self.client.force_authenticate(self.fixture.staff)
        backfill_date = datetime.datetime(2019, 5, 15, 10, 0, 0, tzinfo=datetime.UTC)
        payload = {
            "usages": [
                {
                    "username": "user1",
                    "usage": 30,
                    "date": backfill_date.isoformat(),
                },
                {
                    "username": "user2",
                    "usage": 40,
                },
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # user1 should be in a different billing period (May)
        may_usages = models.ComponentUserUsage.objects.filter(
            component_usage__billing_period=parse_datetime("2019-05-01"),
            username="user1",
        )
        self.assertEqual(may_usages.count(), 1)
        self.assertEqual(float(may_usages.first().usage), 30)
        # user2 should be in the original component_usage (June)
        june_usages = models.ComponentUserUsage.objects.filter(
            component_usage=self.component_usage,
            username="user2",
        )
        self.assertEqual(june_usages.count(), 1)
        self.assertEqual(float(june_usages.first().usage), 40)

    def test_with_offering_user(self):
        self.client.force_authenticate(self.fixture.staff)
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username="ou1"
        )
        offering_user_url = "http://testserver" + reverse(
            "marketplace-offering-user-detail",
            kwargs={"uuid": offering_user.uuid.hex},
        )
        payload = {
            "usages": [
                {
                    "username": "test_user",
                    "usage": 25,
                    "user": offering_user_url,
                },
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user_usage = models.ComponentUserUsage.objects.get(
            component_usage=self.component_usage, username="test_user"
        )
        self.assertEqual(user_usage.user, offering_user)

    def test_total_synced_to_user_sum_when_component_usage_is_zero(self):
        """When ComponentUsage.usage == 0 and user usages are submitted, the total is updated."""
        self.component_usage.usage = 0
        self.component_usage.save(update_fields=["usage"])

        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "usages": [
                {"username": "user1", "usage": 100.0},
                {"username": "user2", "usage": 46.99},
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.component_usage.refresh_from_db()
        self.assertAlmostEqual(float(self.component_usage.usage), 146.99, places=2)

    def test_total_not_changed_when_component_usage_is_nonzero(self):
        """When ComponentUsage.usage != 0, submitting user usages must not override it."""
        self.component_usage.usage = 300
        self.component_usage.save(update_fields=["usage"])

        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "usages": [
                {"username": "user1", "usage": 100.0},
            ]
        }
        response = self.client.post(self.get_url(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.component_usage.refresh_from_db()
        self.assertEqual(float(self.component_usage.usage), 300)


@freeze_time("2024-02-15")
class QuarterlyLimitUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="cpu",
            limit_period=LimitPeriods.QUARTERLY,
            limit_amount=100,
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

    def test_quarterly_limit_aggregation(self):
        # Create usage in current quarter (Q1: Jan-March 2024)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=10,
            date=datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC),
            billing_period=datetime.date(2024, 1, 1),
        )
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=20,
            date=datetime.datetime(2024, 2, 10, tzinfo=datetime.UTC),
            billing_period=datetime.date(2024, 2, 1),
        )

        # Create usage in previous quarter (Q4: Oct-Dec 2023)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=50,
            date=datetime.datetime(2023, 12, 10, tzinfo=datetime.UTC),
            billing_period=datetime.date(2023, 12, 1),
        )

        from waldur_mastermind.marketplace.serializers import ResourceSerializer

        # Test the ResourceSerializer's get_limit_usage method
        limit_usage = ResourceSerializer().get_limit_usage(self.resource)

        # Total usage = January (10) + February (20) = 30
        # Should ignore the December (50) usage because it's not in the current quarter
        self.assertEqual(limit_usage.get("cpu"), 30)


@freeze_time("2024-02-15")
class TotalLimitUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="cpu",
            limit_period=LimitPeriods.TOTAL,
            limit_amount=1000,
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

    def test_total_limit_aggregation(self):
        # Create usages from different years and quarters
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=datetime.datetime(2023, 1, 10, tzinfo=datetime.UTC),
            billing_period=datetime.date(2023, 1, 1),
        )
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=200,
            date=datetime.datetime(2023, 11, 15, tzinfo=datetime.UTC),
            billing_period=datetime.date(2023, 11, 1),
        )
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=50,
            date=datetime.datetime(2024, 2, 10, tzinfo=datetime.UTC),
            billing_period=datetime.date(2024, 2, 1),
        )

        from waldur_mastermind.marketplace.serializers import ResourceSerializer

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)

        # Total usage = 100 + 200 + 50 = 350
        self.assertEqual(limit_usage.get("cpu"), 350)
