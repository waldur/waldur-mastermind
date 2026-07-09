"""
Tests for project cost policies narrowed to a single resource and for the
``use_credit`` toggle that controls whether credit is factored into the limit.
"""

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories


@override_settings(task_always_eager=True)
@freeze_time("2026-04-01")
class ResourceScopedCostMeasurementTest(test.APITestCase):
    """When a policy is scoped to a resource, only that resource's invoice
    items count towards the limit."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            month=4,
            year=2026,
            tax_percent=0,
        )
        self.resource_a = marketplace_factories.ResourceFactory(project=self.project)
        self.resource_b = marketplace_factories.ResourceFactory(project=self.project)

    def _create_policy(self, resource=None, limit_cost=100, use_credit=True):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            resource=resource,
            limit_cost=limit_cost,
            use_credit=use_credit,
            actions="request_pausing",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
        )

    def _create_item(self, resource, unit_price):
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=resource,
            unit_price=unit_price,
            quantity=1,
        )

    def test_only_scoped_resource_cost_counts(self):
        # resource_b is expensive, resource_a is cheap.
        self._create_item(self.resource_a, unit_price=10)
        self._create_item(self.resource_b, unit_price=500)

        policy = self._create_policy(resource=self.resource_a, limit_cost=100)
        self.assertFalse(
            policy.is_triggered(),
            "Policy scoped to the cheap resource must ignore the expensive one.",
        )

    def test_scoped_resource_over_limit_triggers(self):
        self._create_item(self.resource_a, unit_price=500)

        policy = self._create_policy(resource=self.resource_a, limit_cost=100)
        self.assertTrue(
            policy.is_triggered(),
            "Policy must fire when the scoped resource exceeds the limit.",
        )

    def test_project_scoped_still_sums_all_resources(self):
        self._create_item(self.resource_a, unit_price=60)
        self._create_item(self.resource_b, unit_price=60)

        policy = self._create_policy(resource=None, limit_cost=100)
        self.assertTrue(
            policy.is_triggered(),
            "Unscoped policy must sum every resource in the project.",
        )


@override_settings(task_always_eager=True)
@freeze_time("2026-04-01")
class UseCreditToggleTest(test.APITestCase):
    """The ``use_credit`` flag controls whether credit (compensation +
    balance override) is factored into the limit check."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            month=4,
            year=2026,
            tax_percent=0,
        )
        # Ample credit that would normally suppress the trigger.
        invoices_factories.CustomerCreditFactory(
            customer=self.customer,
            value=100000,
        )

    def _create_policy(self, use_credit):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=100,
            use_credit=use_credit,
            actions="request_pausing",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
        )

    def _create_item(self, unit_price):
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=unit_price,
            quantity=1,
        )

    def test_credit_suppresses_trigger_when_enabled(self):
        self._create_item(unit_price=200)
        policy = self._create_policy(use_credit=True)
        self.assertFalse(
            policy.is_triggered(),
            "With use_credit=True, ample customer credit must suppress the trigger.",
        )

    def test_credit_ignored_when_disabled(self):
        self._create_item(unit_price=200)
        policy = self._create_policy(use_credit=False)
        self.assertTrue(
            policy.is_triggered(),
            "With use_credit=False, credit must be ignored and raw cost enforced.",
        )

    def test_below_limit_not_triggered_when_credit_disabled(self):
        self._create_item(unit_price=50)
        policy = self._create_policy(use_credit=False)
        self.assertFalse(
            policy.is_triggered(),
            "Raw cost below the limit must not trigger even with credit disabled.",
        )


@override_settings(task_always_eager=True)
class ResourceScopedPolicyApiTest(test.APITestCase):
    """Serializer/API validation for the resource scope."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.owner = self.fixture.owner
        self.resource = marketplace_factories.ResourceFactory(project=self.project)
        self.url = policy_factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    def _payload(self, **overrides):
        payload = {
            "scope": structure_factories.ProjectFactory.get_url(self.project),
            "limit_cost": 100,
            "period": ProjectEstimatedCostPolicy.Periods.TOTAL,
            "actions": "request_pausing",
            "resource": self.resource.uuid.hex,
        }
        payload.update(overrides)
        return payload

    def test_create_resource_scoped_policy(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(self.url, self._payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.resource, self.resource)
        self.assertTrue(policy.use_credit)

    def test_reject_block_creation_action_when_resource_scoped(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            self.url,
            self._payload(actions="request_pausing,block_creation_of_new_resources"),
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("actions", response.data)

    def test_reject_resource_from_another_project(self):
        other_resource = marketplace_factories.ResourceFactory()
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            self.url,
            self._payload(resource=other_resource.uuid.hex),
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("resource", response.data)

    def test_create_with_use_credit_false(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            self.url, self._payload(resource=None, use_credit=False)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
        self.assertFalse(policy.use_credit)
        self.assertIsNone(policy.resource)
