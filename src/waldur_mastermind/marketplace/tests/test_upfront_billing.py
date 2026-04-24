"""
Tests for per-component billing configuration on OpenStack offerings.

Two billing models for the same OpenStack.Tenant offering type:
1. Monthly LIMIT: billing_type=LIMIT, limit_period=MONTH (default, existing behavior)
2. Prepaid ONE_TIME: billing_type=ONE_TIME, is_prepaid=True (upfront for full duration)

The billing model is configured per-offering by editing component billing_type.
"""

import datetime
from decimal import Decimal
from unittest import mock

from ddt import data, ddt, unpack
from django.test import TestCase
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    RANCHER_OFFERING,
    SITE_AGENT_OFFERING,
    SLURM_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories

# ── 1. get_limit_components includes prepaid ONE_TIME ─────────────────


class GetLimitComponentsTest(TestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
        )

    def test_includes_limit_components(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="cpu",
        )
        result = self.offering.get_limit_components()
        self.assertIn("cpu", result)

    def test_includes_prepaid_one_time_components(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            type="cpu",
        )
        result = self.offering.get_limit_components()
        self.assertIn("cpu", result)

    def test_excludes_non_prepaid_one_time(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=False,
            type="setup_fee",
        )
        result = self.offering.get_limit_components()
        self.assertNotIn("setup_fee", result)

    def test_excludes_fixed_components(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.FIXED,
            type="management_fee",
        )
        result = self.offering.get_limit_components()
        self.assertNotIn("management_fee", result)


# ── 2. Plan.get_estimate works with both billing types ────────────────


class PlanGetEstimateTest(TestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
        )

    def test_estimate_with_limit_components(self):
        """Standard LIMIT component: cost = price × limit."""
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            type="cpu",
        )
        plan = factories.PlanFactory(offering=self.offering, unit_price=Decimal("0"))
        factories.PlanComponentFactory(
            plan=plan, component=component, price=Decimal("10")
        )
        cost = plan.get_estimate(limits={"cpu": 4})
        self.assertEqual(cost, Decimal("40"))  # 4 × 10

    def test_estimate_with_prepaid_one_time_components(self):
        """Prepaid ONE_TIME: cost = price × limit (same formula, included via get_limit_components)."""
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="cpu",
        )
        plan = factories.PlanFactory(offering=self.offering, unit_price=Decimal("0"))
        factories.PlanComponentFactory(
            plan=plan, component=component, price=Decimal("10")
        )
        cost = plan.get_estimate(limits={"cpu": 4})
        self.assertEqual(cost, Decimal("40"))  # 4 × 10

    def test_estimate_with_total_limit_component(self):
        """LIMIT/TOTAL (e.g., consultancy hours): cost = price × limit."""
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
            type="consultancy",
        )
        plan = factories.PlanFactory(offering=self.offering, unit_price=Decimal("0"))
        factories.PlanComponentFactory(
            plan=plan, component=component, price=Decimal("100")
        )
        cost = plan.get_estimate(limits={"consultancy": 50})
        self.assertEqual(cost, Decimal("5000"))


# ── 3. Monthly LIMIT billing (existing behavior) ──────────────────────


@freeze_time("2024-01-15")
class MonthlyLimitBillingTest(test.APITestCase):
    """Validates that monthly LIMIT billing works unchanged."""

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            type="cpu",
            name="CPU",
        )
        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.component,
            price=Decimal("10"),
        )
        self.project = self.offering.customer.projects.create(name="test-project")

    def test_monthly_billing_creates_invoice_item(self):
        """Resource with LIMIT component gets monthly invoice items."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4},
        )
        resource.set_state_ok()
        resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.project.customer, year=2024, month=1
        )
        items = invoice.items.filter(resource=resource)
        self.assertEqual(items.count(), 1)

    def test_monthly_billing_recurs_on_new_month(self):
        """LIMIT components get re-billed each month."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4},
        )
        resource.set_state_ok()
        resource.save()

        with freeze_time("2024-02-01"):
            create_monthly_invoices()
            feb_invoice = invoices_models.Invoice.objects.get(
                customer=self.project.customer, year=2024, month=2
            )
            feb_items = feb_invoice.items.filter(resource=resource)
            self.assertEqual(feb_items.count(), 1)


# ── 4. Prepaid ONE_TIME billing (new behavior) ────────────────────────


@freeze_time("2024-01-15")
class PrepaidOneTimeBillingTest(test.APITestCase):
    """Validates that ONE_TIME + is_prepaid creates upfront charge with limit × months."""

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="cpu",
            name="CPU",
        )
        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.component,
            price=Decimal("10"),
        )
        self.project = self.offering.customer.projects.create(name="test-project")

    def test_prepaid_creates_single_upfront_item(self):
        """ONE_TIME + is_prepaid creates one invoice item on resource creation."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4},
            end_date=datetime.date(2024, 4, 15),  # 3 months
        )
        resource.set_state_ok()
        resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.project.customer, year=2024, month=1
        )
        items = invoice.items.filter(resource=resource)
        self.assertEqual(items.count(), 1)
        item = items.first()
        # quantity = limit × months = 4 × 3 = 12
        self.assertEqual(item.quantity, 12)
        self.assertEqual(item.unit_price, Decimal("10"))
        self.assertEqual(float(item.total), 120.0)

    def test_prepaid_without_end_date_charges_limit_only(self):
        """Without end_date, prepaid charges just the limit (no month multiplier)."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4},
        )
        resource.set_state_ok()
        resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.project.customer, year=2024, month=1
        )
        items = invoice.items.filter(resource=resource)
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.first().quantity, 4)

    def test_prepaid_not_rebilled_on_new_month(self):
        """ONE_TIME components are NOT re-billed on subsequent months."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4},
            end_date=datetime.date(2024, 4, 15),
        )
        resource.set_state_ok()
        resource.save()

        with freeze_time("2024-02-01"):
            create_monthly_invoices()
            feb_invoice = invoices_models.Invoice.objects.get(
                customer=self.project.customer, year=2024, month=2
            )
            feb_items = feb_invoice.items.filter(resource=resource)
            # ONE_TIME never recurs — 0 items on new month
            self.assertEqual(feb_items.count(), 0)


# ── 5. Mixed billing: prepaid infra + LIMIT/TOTAL consultancy ─────────


@freeze_time("2024-01-15")
class MixedBillingTest(test.APITestCase):
    """
    Offering with:
    - CPU: ONE_TIME + is_prepaid (upfront for full duration)
    - Consultancy: LIMIT/TOTAL (one-time flat charge)
    - Management fee: FIXED (monthly recurring)
    """

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        self.cpu = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="cpu",
        )
        self.consultancy = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
            type="consultancy_hours",
        )
        self.fee = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.FIXED,
            type="management_fee",
        )
        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.cpu, price=Decimal("10")
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.consultancy, price=Decimal("100")
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.fee, price=Decimal("50"), amount=1
        )
        self.project = self.offering.customer.projects.create(name="test-project")

    def test_mixed_billing_on_creation(self):
        """Each component type creates the correct invoice item."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4, "consultancy_hours": 50},
            end_date=datetime.date(2024, 4, 15),
        )
        resource.set_state_ok()
        resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.project.customer, year=2024, month=1
        )
        items = invoice.items.filter(resource=resource)
        # 3 items: prepaid CPU, TOTAL consultancy, FIXED fee
        self.assertEqual(items.count(), 3)

        # CPU: prepaid ONE_TIME → 4 × 3 months = 12 × €10 = €120
        cpu_item = items.get(details__offering_component_type="cpu")
        self.assertEqual(cpu_item.quantity, 12)

        # Consultancy: LIMIT/TOTAL → quantity=50 (one-time, not multiplied)
        consult_item = items.get(details__offering_component_type="consultancy_hours")
        self.assertEqual(consult_item.quantity, 50)

        # Management fee: FIXED → prorated for Jan 15-31
        fee_item = items.get(details__offering_component_type="management_fee")
        self.assertTrue(fee_item.quantity > 0)

    def test_mixed_billing_on_new_month(self):
        """Only FIXED recurs; ONE_TIME and TOTAL don't."""
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"cpu": 4, "consultancy_hours": 50},
            end_date=datetime.date(2024, 4, 15),
        )
        resource.set_state_ok()
        resource.save()

        with freeze_time("2024-02-01"):
            create_monthly_invoices()
            feb_invoice = invoices_models.Invoice.objects.get(
                customer=self.project.customer, year=2024, month=2
            )
            feb_items = feb_invoice.items.filter(resource=resource)
            # Only FIXED management fee recurs
            self.assertEqual(feb_items.count(), 1)
            self.assertEqual(
                feb_items.first().details.get("offering_component_type"),
                "management_fee",
            )


# ── 6. effective_available_limits ─────────────────────────────────────


class EffectiveAvailableLimitsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.customer,
        )

    def test_includes_custom_components(self):
        """Custom LIMIT components appear in effective_available_limits."""
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
            type="consultancy_hours",
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("consultancy_hours", response.data["effective_available_limits"])

    def test_includes_builtin_limits(self):
        """Plugin-registered limits are present."""
        with mock.patch.object(
            plugins.manager,
            "get_available_limits",
            return_value=["cores", "ram"],
        ):
            self.client.force_authenticate(self.fixture.staff)
            url = factories.OfferingFactory.get_url(self.offering)
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            for limit_type in ["cores", "ram"]:
                self.assertIn(limit_type, response.data["effective_available_limits"])


# ── 7. OpenStack component editing ────────────────────────────────────


class OpenStackComponentEditingTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.DRAFT,
            customer=self.fixture.customer,
        )
        self.custom_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
            type="consultancy_hours",
            name="Consultancy Hours",
        )

    def test_custom_component_is_editable(self):
        """Can update billing_type, name etc. on custom component."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="update_offering_component"
        )
        payload = {
            "uuid": self.custom_component.uuid.hex,
            "type": "consultancy_hours",
            "name": "Support Hours",
            "billing_type": BillingTypes.USAGE,
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_builtin_type_name_unit_protected(self):
        """Cannot change type/name/measured_unit on builtin components."""
        builtin_types = [
            c.type for c in plugins.manager.get_components(self.offering.type)
        ]
        if not builtin_types:
            self.skipTest("No builtin components registered")

        builtin_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type=builtin_types[0],
            name="Builtin",
            billing_type=BillingTypes.FIXED,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="update_offering_component"
        )
        payload = {
            "uuid": builtin_component.uuid.hex,
            "type": builtin_types[0],
            "name": "Hacked Name",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_builtin_billing_type_is_changeable(self):
        """CAN change billing_type on builtin components (LIMIT → ONE_TIME for prepaid)."""
        builtin_types = [
            c.type for c in plugins.manager.get_components(self.offering.type)
        ]
        if not builtin_types:
            self.skipTest("No builtin components registered")

        builtin_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type=builtin_types[0],
            name="Builtin",
            billing_type=BillingTypes.LIMIT,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="update_offering_component"
        )
        payload = {
            "uuid": builtin_component.uuid.hex,
            "billing_type": BillingTypes.ONE_TIME,
            "is_prepaid": True,
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


# ── 8. Switch billing mode action ─────────────────────────────────────


class SwitchBillingModeTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.DRAFT,
            customer=self.fixture.customer,
        )
        # Create builtin components (cores, ram, storage)
        builtin_types = [
            c.type for c in plugins.manager.get_components(self.offering.type)
        ]
        self.target_components = []
        for bt in builtin_types:
            comp = factories.OfferingComponentFactory(
                offering=self.offering,
                type=bt,
                billing_type=BillingTypes.LIMIT,
                limit_period=LimitPeriods.MONTH,
            )
            self.target_components.append(comp)

        # Create a custom component
        self.custom_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="consultancy_hours",
            name="Consultancy Hours",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )

    def test_switch_to_prepaid_updates_builtin_components(self):
        """Switching to prepaid sets all builtin components to ONE_TIME + is_prepaid."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="switch_billing_mode"
        )
        response = self.client.post(url, {"billing_mode": "prepaid"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        for comp in self.target_components:
            comp.refresh_from_db()
            self.assertEqual(comp.billing_type, BillingTypes.ONE_TIME)
            self.assertTrue(comp.is_prepaid)

        # Prepaid mode sets termination date as required
        self.offering.refresh_from_db()
        self.assertTrue(
            self.offering.plugin_options.get("is_resource_termination_date_required")
        )

    def test_switch_to_prepaid_does_not_affect_custom_components(self):
        """Custom components remain unchanged after switching billing mode."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="switch_billing_mode"
        )
        self.client.post(url, {"billing_mode": "prepaid"}, format="json")

        self.custom_component.refresh_from_db()
        self.assertEqual(self.custom_component.billing_type, BillingTypes.LIMIT)
        self.assertEqual(self.custom_component.limit_period, LimitPeriods.TOTAL)
        self.assertFalse(self.custom_component.is_prepaid)

    def test_switch_back_to_monthly(self):
        """Switching back to monthly restores LIMIT + MONTH on builtin components."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="switch_billing_mode"
        )
        # First switch to prepaid
        self.client.post(url, {"billing_mode": "prepaid"}, format="json")
        # Then switch back to monthly
        response = self.client.post(url, {"billing_mode": "monthly"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        for comp in self.target_components:
            comp.refresh_from_db()
            self.assertEqual(comp.billing_type, BillingTypes.LIMIT)
            self.assertEqual(comp.limit_period, LimitPeriods.MONTH)
            self.assertFalse(comp.is_prepaid)

        # Monthly mode removes termination date requirement
        self.offering.refresh_from_db()
        self.assertNotIn(
            "is_resource_termination_date_required",
            self.offering.plugin_options,
        )

    def test_switch_blocked_when_active_resources_exist(self):
        """Cannot switch billing mode while resources are active."""
        # Create an active resource
        project = self.fixture.customer.projects.create(name="test")
        resource = factories.ResourceFactory(
            offering=self.offering,
            project=project,
            state=models.Resource.States.OK,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="switch_billing_mode"
        )
        response = self.client.post(url, {"billing_mode": "prepaid"}, format="json")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("active resources", response.data["detail"])

        # Cleanup: terminate the resource, then switch should work
        resource.set_state_terminated()
        resource.save()
        response = self.client.post(url, {"billing_mode": "prepaid"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_switch_includes_volume_type_components(self):
        """In dynamic storage mode, volume type components are also switched."""
        # Add volume type components (simulating dynamic storage mode)
        vol_ssd = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gigabytes_ssd",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        vol_hpc = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gigabytes_hpc",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(
            self.offering, action="switch_billing_mode"
        )
        response = self.client.post(url, {"billing_mode": "prepaid"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Volume types should also be switched
        vol_ssd.refresh_from_db()
        vol_hpc.refresh_from_db()
        self.assertEqual(vol_ssd.billing_type, BillingTypes.ONE_TIME)
        self.assertTrue(vol_ssd.is_prepaid)
        self.assertEqual(vol_hpc.billing_type, BillingTypes.ONE_TIME)
        self.assertTrue(vol_hpc.is_prepaid)

        # Custom component should NOT be switched
        self.custom_component.refresh_from_db()
        self.assertEqual(self.custom_component.billing_type, BillingTypes.LIMIT)


# ── 9. Prepaid OpenStack tenant creation produces correct quotas ───────


class PrepaidTenantCreationTest(TestCase):
    """
    Verifies that an OpenStack.Tenant offering with prepaid (ONE_TIME)
    components still produces correct OpenStack quotas via
    TenantCreateProcessor.get_post_data() and map_limits_to_quotas().
    """

    def test_map_limits_to_quotas_works_with_prepaid_components(self):
        """map_limits_to_quotas reads from limits dict regardless of billing_type."""
        from waldur_mastermind.marketplace_openstack.utils import map_limits_to_quotas

        offering = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        # Create prepaid components (ONE_TIME + is_prepaid)
        factories.OfferingComponentFactory(
            offering=offering,
            type="cores",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )
        factories.OfferingComponentFactory(
            offering=offering,
            type="ram",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )
        factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )

        limits = {"cores": 4, "ram": 8192, "storage": 102400}
        quotas = map_limits_to_quotas(limits, offering)

        self.assertEqual(quotas["vcpu"], 4)
        self.assertEqual(quotas["ram"], 8192)
        self.assertEqual(quotas["storage"], 102400)

    def test_get_estimate_works_with_prepaid_components(self):
        """Plan.get_estimate includes prepaid ONE_TIME in cost calculation."""
        offering = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        cpu = factories.OfferingComponentFactory(
            offering=offering,
            type="cores",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )
        plan = factories.PlanFactory(offering=offering, unit_price=Decimal("0"))
        factories.PlanComponentFactory(plan=plan, component=cpu, price=Decimal("10"))

        cost = plan.get_estimate(limits={"cores": 4})
        self.assertEqual(cost, Decimal("40"))  # 4 × 10

    def test_validate_limits_accepts_prepaid_components(self):
        """Order validation accepts limits for prepaid ONE_TIME components."""
        from waldur_mastermind.marketplace.utils import get_components_map

        offering = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=offering,
            type="cores",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )

        # Should not raise — ONE_TIME+is_prepaid is accepted
        result = get_components_map({"cores": 4}, offering)
        self.assertEqual(len(result), 1)
        component, value = result[0]
        self.assertEqual(component.type, "cores")
        self.assertEqual(value, 4)


# ── 10. Generic billing mode switch across offering types ────────────────

OFFERING_CONFIGS = [
    (OPENSTACK_TENANT_OFFERING, BillingTypes.LIMIT, LimitPeriods.MONTH),
    (RANCHER_OFFERING, BillingTypes.USAGE, ""),
    (SLURM_OFFERING, BillingTypes.USAGE, LimitPeriods.TOTAL),
    (SITE_AGENT_OFFERING, BillingTypes.USAGE, LimitPeriods.TOTAL),
]

MODE_EXPECTATIONS = {
    "monthly": (BillingTypes.LIMIT, False),
    "prepaid": (BillingTypes.ONE_TIME, True),
    "usage": (BillingTypes.USAGE, False),
}


@ddt
class GenericSwitchBillingModeTest(test.APITransactionTestCase):
    def _create_offering_with_components(
        self, offering_type, billing_type, limit_period
    ):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            type=offering_type,
            state=OfferingStates.DRAFT,
            customer=self.fixture.customer,
        )
        builtin_types = plugins.manager.get_component_types(offering_type)
        self.target_components = []
        if builtin_types:
            # Offerings with registered builtin components (OpenStack, Rancher, old SLURM)
            for bt in builtin_types:
                comp = factories.OfferingComponentFactory(
                    offering=self.offering,
                    type=bt,
                    billing_type=billing_type,
                    limit_period=limit_period,
                )
                self.target_components.append(comp)
        else:
            # Generic offerings (site agent) — create components manually
            for ctype in ["cpu", "gpu", "ram"]:
                comp = factories.OfferingComponentFactory(
                    offering=self.offering,
                    type=ctype,
                    name=ctype.upper(),
                    billing_type=billing_type,
                    limit_period=limit_period,
                )
                self.target_components.append(comp)

        self.custom_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="consultancy_hours",
            name="Consultancy Hours",
            billing_type=BillingTypes.FIXED,
        )

    def _get_url(self):
        return factories.OfferingFactory.get_url(
            self.offering, action="switch_billing_mode"
        )

    @data(
        *[
            (cfg[0], cfg[1], cfg[2], mode)
            for cfg in OFFERING_CONFIGS
            for mode in ["monthly", "prepaid", "usage"]
        ]
    )
    @unpack
    def test_switch_to_mode(self, offering_type, default_bt, default_lp, target_mode):
        self._create_offering_with_components(offering_type, default_bt, default_lp)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self._get_url(), {"billing_mode": target_mode}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        expected_bt, expected_prepaid = MODE_EXPECTATIONS[target_mode]
        for comp in self.target_components:
            comp.refresh_from_db()
            self.assertEqual(
                comp.billing_type,
                expected_bt,
                f"{offering_type} component {comp.type}: expected billing_type={expected_bt}, got {comp.billing_type}",
            )
            self.assertEqual(comp.is_prepaid, expected_prepaid)

        self.offering.refresh_from_db()
        if target_mode == "prepaid":
            self.assertTrue(
                self.offering.plugin_options.get(
                    "is_resource_termination_date_required"
                )
            )
        elif offering_type == OPENSTACK_TENANT_OFFERING:
            # OpenStack clears the flag when switching away from prepaid
            self.assertNotIn(
                "is_resource_termination_date_required",
                self.offering.plugin_options,
            )

    @data(
        *[
            cfg
            for cfg in OFFERING_CONFIGS
            if plugins.manager.get_component_types(cfg[0])
        ]
    )
    @unpack
    def test_custom_components_unaffected_for_builtin_offerings(
        self, offering_type, default_bt, default_lp
    ):
        """For offerings with builtin types, custom components are not switched."""
        self._create_offering_with_components(offering_type, default_bt, default_lp)
        self.client.force_authenticate(self.fixture.staff)

        for mode in ["monthly", "prepaid", "usage"]:
            self.client.post(self._get_url(), {"billing_mode": mode}, format="json")
            self.custom_component.refresh_from_db()
            self.assertEqual(
                self.custom_component.billing_type,
                BillingTypes.FIXED,
                f"{offering_type} after switching to {mode}: custom component should remain FIXED",
            )

    def test_all_components_switched_for_generic_offerings(self):
        """For generic offerings (site agent), all components are switched."""
        self._create_offering_with_components(
            SITE_AGENT_OFFERING, BillingTypes.USAGE, LimitPeriods.TOTAL
        )
        self.client.force_authenticate(self.fixture.staff)

        self.client.post(self._get_url(), {"billing_mode": "prepaid"}, format="json")
        self.custom_component.refresh_from_db()
        self.assertEqual(self.custom_component.billing_type, BillingTypes.ONE_TIME)

    @data(*OFFERING_CONFIGS)
    @unpack
    def test_blocked_by_active_resources(self, offering_type, default_bt, default_lp):
        self._create_offering_with_components(offering_type, default_bt, default_lp)
        project = self.fixture.customer.projects.create(name="test")
        factories.ResourceFactory(
            offering=self.offering,
            project=project,
            state=models.Resource.States.OK,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._get_url(), {"billing_mode": "prepaid"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("active resources", response.data["detail"])

    @data(*OFFERING_CONFIGS)
    @unpack
    def test_round_trip(self, offering_type, default_bt, default_lp):
        """Test switching through available modes and back."""
        self._create_offering_with_components(offering_type, default_bt, default_lp)
        self.client.force_authenticate(self.fixture.staff)

        for mode in ["prepaid", "usage", "monthly"]:
            response = self.client.post(
                self._get_url(), {"billing_mode": mode}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

            expected_bt, expected_prepaid = MODE_EXPECTATIONS[mode]
            for comp in self.target_components:
                comp.refresh_from_db()
                self.assertEqual(comp.billing_type, expected_bt)
                self.assertEqual(comp.is_prepaid, expected_prepaid)

    def test_offering_without_builtin_components_rejected(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.DRAFT,
            customer=self.fixture.customer,
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.OfferingFactory.get_url(
                self.offering, action="switch_billing_mode"
            ),
            {"billing_mode": "prepaid"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at least one component", response.data["detail"])

    def test_openstack_measured_units_updated_for_usage_mode(self):
        self._create_offering_with_components(
            OPENSTACK_TENANT_OFFERING, BillingTypes.LIMIT, LimitPeriods.MONTH
        )
        self.client.force_authenticate(self.fixture.staff)

        # Switch to usage mode
        response = self.client.post(
            self._get_url(), {"billing_mode": "usage"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for comp in self.target_components:
            comp.refresh_from_db()
            if comp.type == "cores":
                self.assertEqual(comp.measured_unit, "core-hours")
            else:
                self.assertEqual(comp.measured_unit, "GB-hours")

        # Switch back to monthly — units should be restored
        response = self.client.post(
            self._get_url(), {"billing_mode": "monthly"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for comp in self.target_components:
            comp.refresh_from_db()
            if comp.type == "cores":
                self.assertEqual(comp.measured_unit, "cores")
            else:
                self.assertEqual(comp.measured_unit, "GB")
