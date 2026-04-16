import datetime

from ddt import data, ddt, unpack
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory


@ddt
class SlurmPeriodicUsagePolicyGetTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()

        # Add user as owner to the customer
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url()

    @data("staff")
    def test_staff_can_get_policy(self, user_type):
        if user_type == "staff":
            user = structure_factories.UserFactory(is_staff=True)
        else:
            user = self.user

        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_offering_owner_can_get_policy(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unauthorized_user_cannot_get_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class SlurmPeriodicUsagePolicyCreateTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        # Create organization group for the policy
        self.organization_group = structure_factories.OrganizationGroupFactory()

        # Create offering component for component limits
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="nodeHours"
        )

        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url()

    def _create_policy_payload(self):
        return {
            "actions": "notify_organization_owners",
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "organization_groups": [
                structure_factories.OrganizationGroupFactory.get_url(
                    self.organization_group
                )
            ],
            "component_limits_set": [
                {"type": self.offering_component.type, "limit": 1000}
            ],
            "limit_type": "GrpTRESMins",
            "tres_billing_enabled": True,
            "grace_ratio": 0.2,
            "carryover_enabled": True,
            "carryover_factor": 15,
            "raw_usage_reset": True,
            "qos_strategy": "threshold",
        }

    def test_staff_can_create_policy(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        payload = self._create_policy_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        policy = SlurmPeriodicUsagePolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.limit_type, "GrpTRESMins")
        self.assertTrue(policy.tres_billing_enabled)
        self.assertEqual(policy.grace_ratio, 0.2)

    def test_offering_owner_can_create_policy(self):
        self.client.force_authenticate(self.user)

        payload = self._create_policy_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unauthorized_user_cannot_create_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        payload = self._create_policy_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        payload = self._create_policy_payload()
        payload["actions"] = "notify_organization_owners,non_existent_action"

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class SlurmPeriodicUsagePolicyUpdateTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(self.policy)

    def test_staff_can_update_policy(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        payload = {"grace_ratio": 0.3}
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.policy.refresh_from_db()
        self.assertEqual(self.policy.grace_ratio, 0.3)

    def test_offering_owner_can_update_policy(self):
        self.client.force_authenticate(self.user)

        payload = {"carryover_enabled": False}
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.policy.refresh_from_db()
        self.assertFalse(self.policy.carryover_enabled)

    def test_unauthorized_user_cannot_update_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        payload = {"grace_ratio": 0.5}
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class SlurmPeriodicUsagePolicyDeleteTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(self.policy)

    def test_staff_can_delete_policy(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_offering_owner_can_delete_policy(self):
        self.client.force_authenticate(self.user)

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthorized_user_cannot_delete_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SlurmPeriodicUsagePolicyActionsTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url("actions")

    def test_get_available_actions(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        self.assertIn("notify_organization_owners", response.data)


@ddt
class SlurmPeriodicUsagePolicyPreviewImpactTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)
        # Staff user for preview_impact tests (requires is_staff permission)
        self.staff_user = structure_factories.UserFactory(is_staff=True)

        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url("preview_impact")

    def _get_preview_payload(self, **kwargs):
        payload = {
            "allocation": 1000,
            "grace_ratio": 0.2,
            "previous_usage": 0,
            "carryover_factor": 15,
            "carryover_enabled": True,
            "days_elapsed": 90,
            "current_usage": 500,
            "daily_usage_rate": 10,
        }
        payload.update(kwargs)
        return payload

    def test_staff_can_preview_impact(self):
        """Staff users can preview policy impact."""
        self.client.force_authenticate(self.staff_user)

        payload = self._get_preview_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify response structure
        self.assertIn("thresholds", response.data)
        self.assertIn("usage_percentage", response.data)
        self.assertIn("current_qos_status", response.data)
        self.assertIn("preview_commands", response.data)
        self.assertIn("command_history", response.data)

    def test_offering_owner_without_staff_cannot_preview_impact(self):
        """Offering owners without staff privileges cannot preview impact."""
        self.client.force_authenticate(self.user)

        payload = self._get_preview_payload()
        response = self.client.post(self.url, payload)
        # preview_impact requires staff permission since it's a non-detail action
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthorized_user_cannot_preview_impact(self):
        """Regular users cannot preview policy impact."""
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        payload = self._get_preview_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_cannot_preview_impact(self):
        payload = self._get_preview_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_preview_with_zero_allocation(self):
        self.client.force_authenticate(self.staff_user)

        payload = self._get_preview_payload(allocation=0)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_preview_with_carryover_disabled(self):
        self.client.force_authenticate(self.staff_user)

        payload = self._get_preview_payload(carryover_enabled=False, previous_usage=200)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # With carryover disabled, previous usage should not affect carryover
        self.assertEqual(response.data.get("carryover_amount", 0), 0)

    def test_preview_qos_status_normal(self):
        self.client.force_authenticate(self.staff_user)

        # Usage well below threshold
        payload = self._get_preview_payload(
            allocation=1000, current_usage=100, grace_ratio=0.2
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_qos_status"], "normal")

    def test_preview_qos_status_slowdown(self):
        self.client.force_authenticate(self.staff_user)

        # Usage above threshold but below grace limit (slowdown zone)
        payload = self._get_preview_payload(
            allocation=1000, current_usage=1100, grace_ratio=0.2
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_qos_status"], "slowdown")

    def test_preview_qos_status_blocked(self):
        self.client.force_authenticate(self.staff_user)

        # Usage above grace limit (1000 * 1.2 = 1200) - blocked zone
        payload = self._get_preview_payload(
            allocation=1000, current_usage=1300, grace_ratio=0.2
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_qos_status"], "blocked")

    def test_preview_with_invalid_resource_uuid(self):
        self.client.force_authenticate(self.staff_user)

        payload = self._get_preview_payload(
            resource_uuid="00000000-0000-0000-0000-000000000000"
        )
        response = self.client.post(self.url, payload)
        # Should succeed but use provided values since resource not found
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        {"allocation": -100},
        {"grace_ratio": -0.5},
    )
    def test_preview_with_invalid_values(self, invalid_payload):
        self.client.force_authenticate(self.staff_user)

        payload = self._get_preview_payload(**invalid_payload)
        response = self.client.post(self.url, payload)
        # Should either validate and reject or handle gracefully
        self.assertIn(
            response.status_code,
            [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST],
        )


@ddt
class SlurmPeriodicUsagePolicyPeriodValidationTest(test.APITestCase):
    """Validate that policy.period must match the offering component's limit_period."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url()

    def _create_limit_component(self, limit_period):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=limit_period,
        )

    def _base_payload(self, **overrides):
        payload = {
            "actions": "notify_organization_owners",
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "apply_to_all": True,
            "limit_type": "GrpTRESMins",
            "tres_billing_enabled": True,
            "grace_ratio": 0.2,
            "carryover_enabled": True,
            "carryover_factor": 15,
            "raw_usage_reset": True,
            "qos_strategy": "threshold",
            "component_limits_set": [],
        }
        payload.update(overrides)
        return payload

    def test_create_with_matching_period_succeeds(self):
        self._create_limit_component(LimitPeriods.MONTH)
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=PeriodMixin.Periods.MONTH_1)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_with_mismatched_period_fails(self):
        self._create_limit_component(LimitPeriods.MONTH)
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=PeriodMixin.Periods.MONTH_3)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("period", response.data)

    def test_create_without_period_auto_sets_from_component(self):
        self._create_limit_component(LimitPeriods.QUARTERLY)
        self.client.force_authenticate(self.staff)
        payload = self._base_payload()
        # Do not include period in payload
        payload.pop("period", None)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = SlurmPeriodicUsagePolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.period, PeriodMixin.Periods.MONTH_3)

    def test_create_with_no_limit_component_allows_any_period(self):
        # Offering has no limit-based component
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=PeriodMixin.Periods.MONTH_3)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_update_period_to_mismatched_value_fails(self):
        self._create_limit_component(LimitPeriods.MONTH)
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
        )
        detail_url = SlurmPeriodicUsagePolicyFactory.get_url(policy)
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            detail_url, {"period": PeriodMixin.Periods.MONTH_3}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("period", response.data)

    def test_update_period_to_matching_value_succeeds(self):
        self._create_limit_component(LimitPeriods.QUARTERLY)
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
        )
        detail_url = SlurmPeriodicUsagePolicyFactory.get_url(policy)
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            detail_url, {"period": PeriodMixin.Periods.MONTH_3}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        policy.refresh_from_db()
        self.assertEqual(policy.period, PeriodMixin.Periods.MONTH_3)

    @data(
        (LimitPeriods.MONTH, PeriodMixin.Periods.MONTH_1),
        (LimitPeriods.QUARTERLY, PeriodMixin.Periods.MONTH_3),
        (LimitPeriods.ANNUAL, PeriodMixin.Periods.MONTH_12),
        (LimitPeriods.TOTAL, PeriodMixin.Periods.TOTAL),
    )
    @unpack
    def test_all_limit_period_mappings(self, limit_period, expected_policy_period):
        self._create_limit_component(limit_period)
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=expected_policy_period)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = SlurmPeriodicUsagePolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.period, expected_policy_period)

    @data(
        (LimitPeriods.MONTH, PeriodMixin.Periods.MONTH_3),
        (LimitPeriods.MONTH, PeriodMixin.Periods.MONTH_12),
        (LimitPeriods.QUARTERLY, PeriodMixin.Periods.MONTH_1),
        (LimitPeriods.ANNUAL, PeriodMixin.Periods.MONTH_1),
    )
    @unpack
    def test_all_limit_period_mismatches_rejected(self, limit_period, wrong_period):
        self._create_limit_component(limit_period)
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=wrong_period)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("period", response.data)

    def test_multiple_components_same_limit_period_succeeds(self):
        """Two limit-based components with the same limit_period should work."""
        self._create_limit_component(LimitPeriods.MONTH)
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=PeriodMixin.Periods.MONTH_1)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_multiple_components_different_limit_periods_rejected(self):
        """Two limit-based components with different limit_periods should fail."""
        self._create_limit_component(LimitPeriods.MONTH)
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.QUARTERLY,
        )
        self.client.force_authenticate(self.staff)
        payload = self._base_payload(period=PeriodMixin.Periods.MONTH_1)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("period", response.data)


@freeze_time("2026-03-15")
class SlurmPolicyGetCurrentPeriodFromComponentTest(test.APITransactionTestCase):
    """_get_current_period() should derive period from component, not DB field."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)

    def _create_limit_component(self, limit_period):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=limit_period,
        )

    def test_uses_component_period_over_stale_db_value(self):
        """When DB has MONTH_1 but component says quarterly, use quarterly."""
        self._create_limit_component(LimitPeriods.QUARTERLY)
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,  # stale DB value
        )
        # Should return quarterly format, not monthly
        result = policy._get_current_period()
        self.assertEqual(result, "2026-Q1")

    def test_falls_back_to_db_when_no_limit_component(self):
        """When no limit-based component, use the DB period field."""
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
        )
        result = policy._get_current_period()
        self.assertEqual(result, "2026-03")

    def test_monthly_component_returns_monthly_format(self):
        self._create_limit_component(LimitPeriods.MONTH)
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
        )
        result = policy._get_current_period()
        self.assertEqual(result, "2026-03")

    def test_annual_component_returns_annual_format(self):
        self._create_limit_component(LimitPeriods.ANNUAL)
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_12,
        )
        result = policy._get_current_period()
        self.assertEqual(result, "2026")

    def test_total_component_returns_total(self):
        self._create_limit_component(LimitPeriods.TOTAL)
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.TOTAL,
        )
        result = policy._get_current_period()
        self.assertEqual(result, "total")

    def test_conflicting_components_falls_back_to_db(self):
        """When components have different limit_periods, fall back to DB."""
        self._create_limit_component(LimitPeriods.MONTH)
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.QUARTERLY,
        )
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
        )
        # Ambiguous components → falls back to DB period (MONTH_1)
        result = policy._get_current_period()
        self.assertEqual(result, "2026-03")


class SlurmPeriodicUsagePolicyPreviewPeriodTest(test.APITestCase):
    """Test that preview_impact respects the policy's period setting."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.customer, type="Marketplace.Slurm"
        )
        self.component = marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="node",
            name="Compute",
            measured_unit="node hours",
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            limits={"node": 1},
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.resource.plan = self.plan
        self.resource.save()

        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url("preview_impact")

    def _create_usage(self, billing_period, usage_value):
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            billing_period=billing_period,
            usage=usage_value,
            date=billing_period,
        )

    @freeze_time("2026-03-15")
    def test_monthly_policy_preview_uses_current_month_only(self):
        """Preview with monthly policy should only count current month's usage."""

        SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
            grace_ratio=0.15,
        )

        # Feb usage should NOT be counted for monthly policy
        self._create_usage(datetime.date(2026, 2, 1), 1.08)
        # March usage should be counted
        self._create_usage(datetime.date(2026, 3, 1), 0.36)

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url,
            {
                "allocation": 1,
                "grace_ratio": 0.15,
                "carryover_enabled": False,
                "carryover_factor": 0,
                "previous_usage": 0,
                "resource_uuid": str(self.resource.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should use only March usage (0.36), not Feb+Mar (1.44)
        self.assertAlmostEqual(response.data["current_usage"], 0.36, places=2)
        self.assertEqual(response.data["current_qos_status"], "normal")

    @freeze_time("2026-03-15")
    def test_quarterly_policy_preview_sums_all_months_in_quarter(self):
        """Preview with quarterly policy should sum all months in the quarter."""

        SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_3,
            grace_ratio=0.15,
        )

        self._create_usage(datetime.date(2026, 1, 1), 0.20)
        self._create_usage(datetime.date(2026, 2, 1), 0.30)
        self._create_usage(datetime.date(2026, 3, 1), 0.10)

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url,
            {
                "allocation": 1,
                "grace_ratio": 0.15,
                "carryover_enabled": False,
                "carryover_factor": 0,
                "previous_usage": 0,
                "resource_uuid": str(self.resource.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should sum all Q1 months: 0.20 + 0.30 + 0.10 = 0.60
        self.assertAlmostEqual(response.data["current_usage"], 0.60, places=2)

    @freeze_time("2026-03-15")
    def test_monthly_policy_preview_not_blocked_when_quarterly_would_be(self):
        """Monthly policy should show normal when only current month is under limit,
        even if quarterly sum would exceed it."""

        SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_1,
            grace_ratio=0.15,
        )

        # Previous month: heavy usage that would push quarterly total over limit
        self._create_usage(datetime.date(2026, 2, 1), 1.08)
        # Current month: under limit
        self._create_usage(datetime.date(2026, 3, 1), 0.36)

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url,
            {
                "allocation": 1,
                "grace_ratio": 0.15,
                "carryover_enabled": False,
                "carryover_factor": 0,
                "previous_usage": 0,
                "resource_uuid": str(self.resource.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Monthly: 0.36 < 1.0 allocation → normal (not blocked)
        self.assertEqual(response.data["current_qos_status"], "normal")
        # Quarterly would have been: 1.44 > 1.15 blocked_threshold → blocked
        # But monthly policy should NOT show that
        self.assertAlmostEqual(response.data["current_usage"], 0.36, places=2)

    @freeze_time("2026-03-15")
    def test_total_policy_preview_sums_all_usage(self):
        """Preview with TOTAL period should sum usage across all time."""

        SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.TOTAL,
            grace_ratio=0.15,
        )

        # Usage from various months spanning multiple quarters/years
        self._create_usage(datetime.date(2025, 6, 1), 0.10)
        self._create_usage(datetime.date(2025, 12, 1), 0.20)
        self._create_usage(datetime.date(2026, 1, 1), 0.30)
        self._create_usage(datetime.date(2026, 3, 1), 0.40)

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url,
            {
                "allocation": 2,
                "grace_ratio": 0.15,
                "carryover_enabled": False,
                "carryover_factor": 0,
                "previous_usage": 0,
                "resource_uuid": str(self.resource.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should sum ALL usage: 0.10 + 0.20 + 0.30 + 0.40 = 1.00
        self.assertAlmostEqual(response.data["current_usage"], 1.00, places=2)

    @freeze_time("2026-03-15")
    def test_total_policy_preview_has_no_carryover(self):
        """Preview with TOTAL period should not apply carryover since there's no previous period."""

        SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.TOTAL,
            grace_ratio=0.15,
            carryover_enabled=True,
            carryover_factor=50,
        )

        self._create_usage(datetime.date(2025, 6, 1), 0.50)
        self._create_usage(datetime.date(2026, 3, 1), 0.30)

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url,
            {
                "allocation": 2,
                "grace_ratio": 0.15,
                "carryover_enabled": True,
                "carryover_factor": 50,
                "previous_usage": 0,
                "resource_uuid": str(self.resource.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # TOTAL has no previous period, so effective_allocation == base_allocation (no carryover)
        self.assertEqual(
            response.data["effective_allocation"],
            response.data["base_allocation"],
        )

    @freeze_time("2026-03-15")
    def test_annual_policy_preview_sums_current_year(self):
        """Preview with MONTH_12 period should sum usage for the current year only."""

        SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.MONTH_12,
            grace_ratio=0.15,
        )

        # Previous year usage should NOT be counted
        self._create_usage(datetime.date(2025, 11, 1), 0.50)
        # Current year usage should be counted
        self._create_usage(datetime.date(2026, 1, 1), 0.20)
        self._create_usage(datetime.date(2026, 3, 1), 0.30)

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url,
            {
                "allocation": 2,
                "grace_ratio": 0.15,
                "carryover_enabled": False,
                "carryover_factor": 0,
                "previous_usage": 0,
                "resource_uuid": str(self.resource.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should sum only 2026 usage: 0.20 + 0.30 = 0.50
        self.assertAlmostEqual(response.data["current_usage"], 0.50, places=2)


class SlurmPeriodicUsagePolicyTotalPeriodCalculationTest(test.APITestCase):
    """Test that _get_period_usage returns all usage for TOTAL period."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.customer, type="Marketplace.Slurm"
        )
        self.component = marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="cpu",
            name="CPU",
            measured_unit="hours",
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            limits={"cpu": 1000},
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.resource.plan = self.plan
        self.resource.save()

    def _create_usage(self, billing_period, usage_value):
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            billing_period=billing_period,
            usage=usage_value,
            date=billing_period,
        )

    def test_get_period_usage_returns_all_usage_for_total(self):
        """_get_period_usage with 'total' should return all usage across all time."""
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.TOTAL,
        )

        self._create_usage(datetime.date(2024, 1, 1), 100)
        self._create_usage(datetime.date(2025, 6, 1), 200)
        self._create_usage(datetime.date(2026, 3, 1), 300)

        usage = policy._get_period_usage(self.resource, "total")
        self.assertEqual(usage["cpu"], 600.0)

    def test_get_resource_usage_percentage_nonzero_for_total(self):
        """get_resource_usage_percentage should report real usage for TOTAL period."""
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering,
            period=PeriodMixin.Periods.TOTAL,
            carryover_enabled=False,
        )

        self._create_usage(datetime.date(2024, 1, 1), 500)
        self._create_usage(datetime.date(2026, 3, 1), 300)

        pct = policy.get_resource_usage_percentage(self.resource)
        # 800 / 1000 = 80%
        self.assertAlmostEqual(pct, 80.0, places=1)
