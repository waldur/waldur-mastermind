from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    BillingTypes,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories


class ChildOfferingBase(test.APITestCase):
    """A tenant offering and the per-tenant Instance offering made under it."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.parent = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            customer=self.fixture.customer,
        )
        self.parent_component = factories.OfferingComponentFactory(
            offering=self.parent,
            type="cores",
            billing_type=BillingTypes.LIMIT,
            billed_per_plan=True,
        )
        self.plan = factories.PlanFactory(offering=self.parent)
        factories.PlanComponentFactory(plan=self.plan, component=self.parent_component)
        # Mirrors create_offerings_for_volume_and_instance.
        self.child = factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            customer=self.fixture.customer,
            parent=self.parent,
            billable=False,
            shared=False,
        )
        self.client.force_authenticate(self.fixture.staff)


class ChildOfferingComponentManagementTest(ChildOfferingBase):
    def post(self, action, payload):
        return self.client.post(
            factories.OfferingFactory.get_url(self.child, action), payload
        )

    def test_a_component_cannot_be_added_to_a_child_offering(self):
        response = self.post(
            "create_offering_component",
            {
                "type": "gpu_hours",
                "name": "GPU hours",
                "measured_unit": "hours",
                "billing_type": BillingTypes.USAGE,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.child.components.exists())

    def test_a_child_offering_component_cannot_be_updated(self):
        component = factories.OfferingComponentFactory(
            offering=self.child, type="gpu_hours", name="GPU hours"
        )

        response = self.post(
            "update_offering_component",
            {
                "uuid": component.uuid.hex,
                "type": "gpu_hours",
                "name": "Renamed",
                "measured_unit": "hours",
                "billing_type": BillingTypes.USAGE,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        component.refresh_from_db()
        self.assertEqual("GPU hours", component.name)

    # A stray component left on a child is refused like any other write:
    # the parent's pricing surfaces are where components are managed.
    def test_a_child_offering_component_cannot_be_removed(self):
        component = factories.OfferingComponentFactory(
            offering=self.child, type="gpu_hours"
        )

        response = self.post("remove_offering_component", {"uuid": component.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self.child.components.filter(pk=component.pk).exists())

    def test_billing_mode_cannot_be_switched_on_a_child_offering(self):
        # Give the child a builtin so only the child-offering rule can refuse.
        component = factories.OfferingComponentFactory(
            offering=self.child,
            type="cores",
            billing_type=BillingTypes.LIMIT,
            billed_per_plan=True,
        )

        response = self.post("switch_billing_mode", {"billing_mode": "usage"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("child offering", str(response.data))
        component.refresh_from_db()
        self.assertEqual(BillingTypes.LIMIT, component.billing_type)

    def test_the_parent_offering_components_can_still_be_managed(self):
        response = self.client.post(
            factories.OfferingFactory.get_url(self.parent, "create_offering_component"),
            {
                "type": "gpu_hours",
                "name": "GPU hours",
                "measured_unit": "hours",
                "billing_type": BillingTypes.USAGE,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@freeze_time("2026-09-11")
class ChildResourceUsageTest(ChildOfferingBase):
    def setUp(self):
        super().setUp()
        self.resource = models.Resource.objects.create(
            offering=self.child, plan=self.plan, project=self.fixture.project
        )
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.get(resource=self.resource)

    def report_usage(self, component_type):
        return self.client.post(
            "/api/marketplace-component-usages/set_usage/",
            {
                "plan_period": self.plan_period.uuid.hex,
                "usages": [{"type": component_type, "amount": 5}],
            },
        )

    def test_usage_for_a_component_only_the_child_has_is_refused(self):
        # validate() used to accept it off the child while save() looks it up
        # on the plan's offering, the parent -- a 500 instead of a 400.
        factories.OfferingComponentFactory(
            offering=self.child,
            type="gpu_hours",
            billing_type=BillingTypes.USAGE,
        )

        response = self.report_usage("gpu_hours")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ComponentUsage.objects.filter(resource=self.resource).exists()
        )

    def test_usage_for_a_component_only_the_parent_has_is_still_refused(self):
        response = self.report_usage("cores")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
