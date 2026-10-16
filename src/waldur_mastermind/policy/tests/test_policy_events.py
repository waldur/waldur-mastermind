from django.test import override_settings
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import CustomerUsagePolicyComponent
from waldur_mastermind.policy.tests import factories
from waldur_mastermind.policy.tests import fixtures as policy_fixtures


@override_settings(task_always_eager=True)
class PolicyEventTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.client.force_authenticate(self.fixture.owner)

    def test_creation_is_logged(self):
        response = self.client.post(
            factories.ProjectEstimatedCostPolicyFactory.get_list_url(),
            {
                "limit_cost": 100,
                "actions": "notify_organization_owners",
                "scope": structure_factories.ProjectFactory.get_url(self.project),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = Event.objects.get(event_type=EventType.POLICY_CREATED)
        self.assertEqual(event.context["policy_uuid"], response.data["uuid"])
        self.assertEqual(event.context["policy_scope_uuid"], self.project.uuid.hex)
        self.assertEqual(event.context["user_uuid"], self.fixture.owner.uuid.hex)

    def test_update_logs_only_values_that_changed(self):
        policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)

        response = self.client.patch(
            factories.ProjectEstimatedCostPolicyFactory.get_url(policy),
            {"limit_cost": 200, "actions": policy.actions},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = Event.objects.get(event_type=EventType.POLICY_UPDATED)
        self.assertEqual(event.context["policy_uuid"], policy.uuid.hex)
        self.assertEqual(event.context["policy_changes"], "limit_cost: 10 -> 200")

    def test_update_without_changes_is_not_logged(self):
        policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)

        self.client.patch(
            factories.ProjectEstimatedCostPolicyFactory.get_url(policy),
            {"actions": policy.actions},
        )

        self.assertFalse(
            Event.objects.filter(event_type=EventType.POLICY_UPDATED).exists()
        )

    def test_deletion_is_logged(self):
        policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)

        response = self.client.delete(
            factories.ProjectEstimatedCostPolicyFactory.get_url(policy)
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        event = Event.objects.get(event_type=EventType.POLICY_DELETED)
        self.assertEqual(event.context["policy_uuid"], policy.uuid.hex)

    def test_background_save_is_not_logged(self):
        policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)

        policy.has_fired = True
        policy.save()

        self.assertFalse(
            Event.objects.filter(
                event_type__in=[EventType.POLICY_CREATED, EventType.POLICY_UPDATED]
            ).exists()
        )


@override_settings(task_always_eager=True)
class UsagePolicyComponentLimitEventTest(test.APITestCase):
    """Component limits are recreated on every update, so they must be
    compared by value."""

    def updates(self):
        return Event.objects.filter(event_type=EventType.POLICY_UPDATED)

    def test_offering_usage_policy_limits_sent_back_unchanged_are_not_logged(self):
        fixture = policy_fixtures.OfferingUsagePolicyFixture()
        url = factories.OfferingUsagePolicyFactory.get_url(fixture.policy)
        component_type = fixture.offering_usage_component.type
        self.client.force_authenticate(fixture.staff)

        self.client.patch(
            url,
            {"component_limits_set": [{"type": component_type, "limit": 10}]},
            format="json",
        )
        self.assertFalse(self.updates().exists())

        self.client.patch(
            url,
            {"component_limits_set": [{"type": component_type, "limit": 20}]},
            format="json",
        )
        changes = self.updates().get().context["policy_changes"]
        self.assertIn("limit=10", changes)
        self.assertIn("limit=20", changes)
        self.assertNotIn("object (", changes)

    def test_customer_usage_policy_limits_sent_back_unchanged_are_not_logged(self):
        fixture = marketplace_fixtures.MarketplaceFixture()
        policy = factories.CustomerComponentUsagePolicyFactory(scope=fixture.customer)
        component = marketplace_factories.OfferingComponentFactory(
            offering=fixture.offering, billing_type=BillingTypes.USAGE, type="cores"
        )
        CustomerUsagePolicyComponent.objects.create(
            policy=policy, component=component, limit=100, period=1
        )
        self.client.force_authenticate(fixture.staff)

        self.client.patch(
            factories.CustomerComponentUsagePolicyFactory.get_url(policy),
            {
                "component_limits_set": [
                    {"component": component.uuid.hex, "limit": 100, "period": 1}
                ]
            },
            format="json",
        )

        self.assertFalse(self.updates().exists())

    def test_limit_moved_to_same_named_component_of_another_offering_is_logged(self):
        fixture = marketplace_fixtures.MarketplaceFixture()
        policy = factories.CustomerComponentUsagePolicyFactory(scope=fixture.customer)
        components = [
            marketplace_factories.OfferingComponentFactory(
                offering=marketplace_factories.OfferingFactory(
                    customer=fixture.customer
                ),
                billing_type=BillingTypes.USAGE,
                type="cores",
                name="CPU",
            )
            for _ in range(2)
        ]
        CustomerUsagePolicyComponent.objects.create(
            policy=policy, component=components[0], limit=100, period=1
        )
        self.client.force_authenticate(fixture.staff)

        response = self.client.patch(
            factories.CustomerComponentUsagePolicyFactory.get_url(policy),
            {
                "component_limits_set": [
                    {"component": components[1].uuid.hex, "limit": 100, "period": 1}
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        changes = self.updates().get().context["policy_changes"]
        self.assertIn(components[0].uuid.hex, changes)
        self.assertIn(components[1].uuid.hex, changes)
