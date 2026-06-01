"""Pre-flight cost policy enforcement at order creation [HPCMP-484].

Reproduces a demo bug where an organization could submit orders that exceed
the cost policy limit. The expected behavior is that the marketplace API
rejects such orders with 400 Bad Request before any Order or Resource row
is persisted.

Three scopes need to be enforced symmetrically (Project, Customer, Offering)
across three order paths:

- CREATE: new order from ``OrderCreateSerializer`` (initial purchase).
- UPDATE — ``update_limits``: scaling an existing resource's limits.
- UPDATE — ``switch_plan``: moving an existing resource to a different plan.
"""

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.tests import factories as policy_factories


class PreflightCostPolicyTest(test.APITestCase):
    """Reproduce HPCMP-484: cost policies must block orders pre-flight."""

    LIMIT_COST = 10
    RESOURCE_COST = 100  # Plan unit_price chosen to exceed LIMIT_COST.

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.customer = self.project.customer

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)

        self.offering = marketplace_factories.OfferingFactory(
            state=OfferingStates.ACTIVE
        )
        # unit_price drives Plan.get_estimate when no limit components exist.
        self.plan = marketplace_factories.PlanFactory(
            offering=self.offering, unit_price=self.RESOURCE_COST
        )

    def _create_order(self, user):
        self.client.force_authenticate(user)
        url = marketplace_factories.OrderFactory.get_list_url()
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.project),
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                self.offering
            ),
            "plan": marketplace_factories.PlanFactory.get_public_url(self.plan),
            "attributes": {},
        }
        return self.client.post(url, payload)

    def _assert_blocked(self, response):
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertFalse(marketplace_models.Order.objects.exists())
        self.assertFalse(marketplace_models.Resource.objects.exists())

    def test_project_cost_policy_blocks_first_order_exceeding_limit(self):
        """Project-scoped policy with `block_creation_of_new_resources`.

        Reproduces the silent-failure mode: the existing pre-flight signal
        runs and raises ``PolicyException``, but ``init_cost`` swallows it,
        so the API responds 201 with a resource pinned to ERRED state.
        """
        policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=self.LIMIT_COST,
            actions="block_creation_of_new_resources",
        )

        response = self._create_order(self.fixture.staff)
        self._assert_blocked(response)

    def test_customer_cost_policy_blocks_first_order_exceeding_limit(self):
        """Customer-scoped policy — the demo scenario.

        No pre-flight check exists today: the policy only fires after an
        invoice item is saved, so the first over-limit order goes through.
        """
        policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=self.LIMIT_COST,
            actions="block_creation_of_new_resources",
        )

        response = self._create_order(self.fixture.staff)
        self._assert_blocked(response)

    def test_offering_cost_policy_blocks_first_order_exceeding_limit(self):
        """Offering-scoped policy — same gap as the customer scope."""
        policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.offering,
            limit_cost=self.LIMIT_COST,
            actions="block_creation_of_new_resources",
        )

        response = self._create_order(self.fixture.staff)
        self._assert_blocked(response)


class PreflightCostPolicyUpdateTest(test.APITestCase):
    """UPDATE / plan-switch orders must respect cost policies too."""

    LIMIT_COST = 50

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.customer = self.project.customer

        CustomerRole.OWNER.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        self.offering = marketplace_factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
        )
        # A limit-based component so update_limits actually changes cost.
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
        )

        self.cheap_plan = marketplace_factories.PlanFactory(
            offering=self.offering, unit_price=10
        )
        self.expensive_plan = marketplace_factories.PlanFactory(
            offering=self.offering, unit_price=200
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.cheap_plan,
            component=self.offering.components.get(type="cpu"),
            price=1,
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.expensive_plan,
            component=self.offering.components.get(type="cpu"),
            price=1,
        )

        # Existing resource sits comfortably under the limit (cost ~ 10).
        self.resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.cheap_plan,
            limits={"cpu": 1},
            state=ResourceStates.OK,
        )
        self.resource.init_cost()
        self.resource.save()

    def _switch_plan(self, plan):
        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.ResourceFactory.get_url(
            self.resource, "switch_plan"
        )
        return self.client.post(
            url, {"plan": marketplace_factories.PlanFactory.get_public_url(plan)}
        )

    def _update_limits(self, limits):
        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.ResourceFactory.get_url(
            self.resource, "update_limits"
        )
        return self.client.post(url, {"limits": limits})

    def _assert_blocked(self, response):
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        # No new order written.
        self.assertEqual(marketplace_models.Order.objects.count(), 0)

    def test_switch_plan_to_expensive_plan_is_blocked(self):
        policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=self.LIMIT_COST,
            actions="block_creation_of_new_resources",
        )
        # New plan estimate (200) far exceeds limit (50).
        response = self._switch_plan(self.expensive_plan)
        self._assert_blocked(response)

    def test_update_limits_above_threshold_is_blocked(self):
        policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=self.LIMIT_COST,
            actions="block_creation_of_new_resources",
        )
        # Scaling cpu from 1 to 100 puts the plan estimate well over the limit.
        response = self._update_limits({"cpu": 100})
        self._assert_blocked(response)

    def test_update_limits_within_threshold_is_allowed(self):
        # A small bump stays under the limit (plan unit_price 10 + 2 cpu * 1 = 12).
        policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=self.LIMIT_COST,
            actions="block_creation_of_new_resources",
        )
        response = self._update_limits({"cpu": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
