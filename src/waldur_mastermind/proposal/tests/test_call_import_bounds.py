"""What an imported call document is held to beyond its fields: component
rules and their pools follow the rules the API holds them to, the document's
lists are bounded, and imports are throttled per user."""

import copy
from unittest import mock

from rest_framework import status, test
from rest_framework.settings import api_settings

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import call_templates, call_transfer, models, views
from waldur_mastermind.proposal.tests import factories, fixtures

COMPONENT = {
    "component_type": "cpu",
    "max_per_proposal": "5",
    "min_per_proposal": None,
    "default_amount": None,
    "is_fixed": False,
    "pools": [{"round": None, "offered_total": "10"}],
}


class ImportedComponentsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        call = self.fixture.call
        requested = factories.RequestedOfferingFactory(call=call)
        offering = requested.offering
        offering.state = marketplace_models.Offering.States.ACTIVE
        offering.shared = True
        offering.save()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering, type="cpu", billing_type=BillingTypes.LIMIT
        )
        marketplace_factories.PlanComponentFactory(
            plan=requested.plan, component=component
        )
        document, _ = call_transfer.export_call(call)
        self.document = copy.deepcopy(document)
        [self.entry] = [
            entry
            for entry in self.document["requested_offerings"]
            if entry["offering"]["name"] == offering.name
        ]
        self.staff = structure_factories.UserFactory(is_staff=True)

    def import_(self):
        return call_transfer.import_call(
            self.document, self.fixture.call.manager, self.staff, name="Copy"
        )

    def rules(self, call):
        return models.CallOfferingComponent.objects.filter(
            requested_offering__call=call
        )

    def test_one_rule_per_component(self):
        self.entry["offering_components"] = [
            COMPONENT,
            dict(COMPONENT, max_per_proposal="999"),
        ]
        call, _, warnings = self.import_()
        [rule] = self.rules(call)
        self.assertEqual(rule.max_per_proposal, 5)
        self.assertTrue(any("component rule left out" in w for w in warnings), warnings)

    def test_one_pool_per_component_and_round(self):
        self.entry["offering_components"] = [
            dict(
                COMPONENT,
                pools=[
                    {"round": None, "offered_total": "10"},
                    {"round": None, "offered_total": "99"},
                    {"round": 999, "offered_total": "99"},
                ],
            )
        ]
        call, _, warnings = self.import_()
        [rule] = self.rules(call)
        self.assertEqual([p.offered_total for p in rule.pools.all()], [10])
        self.assertTrue(any("whole call" in w for w in warnings), warnings)
        self.assertTrue(any("was not imported" in w for w in warnings), warnings)

    def test_a_component_the_plan_does_not_price_is_left_out(self):
        self.entry["offering_components"] = [dict(COMPONENT, component_type="gpu")]
        call, _, warnings = self.import_()
        self.assertFalse(self.rules(call).exists())
        self.assertTrue(any("component rule left out" in w for w in warnings), warnings)

    def test_a_rule_its_resource_templates_fall_outside_of_is_left_out(self):
        self.entry["resource_templates"] = [
            {"name": "Big", "limits": {"cpu": 50}, "description": ""}
        ]
        self.entry["offering_components"] = [COMPONENT]
        call, _, warnings = self.import_()
        self.assertFalse(self.rules(call).exists())
        self.assertFalse(
            models.CallResourcePool.objects.filter(
                offering_component__requested_offering__call=call
            ).exists()
        )
        self.assertTrue(
            any("resource template" in w and "Big" in w for w in warnings), warnings
        )

    def test_a_valid_rule_is_kept_on_the_requested_offering(self):
        self.entry["offering_components"] = [COMPONENT]
        call, _, warnings = self.import_()
        [rule] = self.rules(call)
        self.assertEqual(rule.max_per_proposal, 5)
        self.assertEqual([p.offered_total for p in rule.pools.all()], [10])
        self.assertEqual(warnings, [])


class DocumentBoundsTest(test.APITestCase):
    def setUp(self):
        self.manager = factories.CallManagingOrganisationFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.document = call_templates.load("rapid_access")

    def import_(self):
        return call_transfer.import_call(self.document, self.manager, self.staff)

    def test_a_list_longer_than_its_bound_is_refused(self):
        steps = self.document["workflow_steps"]
        extra = {
            "key": "x",
            "kind": "decision",
            "name": "N",
            "outcome_set": "eligibility",
            "completer": "call_manager",
            "on_negative": "reject",
            "is_enabled": True,
            "notification_rules": [],
        }
        maximum = call_transfer.MAX_LIST_ENTRIES["workflow_steps"]
        self.document["workflow_steps"] = steps + [
            dict(extra, key=f"s{i}", order=100 + i)
            for i in range(maximum + 1 - len(steps))
        ]
        with self.assertRaises(call_transfer.serializers.ValidationError) as raised:
            self.import_()
        self.assertIn("workflow_steps", raised.exception.detail)
        self.assertIn(f"At most {maximum}", str(raised.exception.detail))
        self.assertFalse(models.Call.objects.filter(manager=self.manager).exists())

    def test_nested_lists_are_bounded_too(self):
        maximum = call_transfer.MAX_LIST_ENTRIES["notification_rules"]
        step = self.document["workflow_steps"][0]
        rule = (step.get("notification_rules") or [{}])[0]
        step["notification_rules"] = [rule] * (maximum + 1)
        with self.assertRaises(call_transfer.serializers.ValidationError) as raised:
            self.import_()
        [context] = raised.exception.detail
        self.assertTrue(context.endswith(".notification_rules"), context)

    def test_duplicate_step_keys_are_named(self):
        steps = self.document["workflow_steps"]
        steps.append(dict(steps[0], order=999))
        with self.assertRaises(call_transfer.serializers.ValidationError) as raised:
            self.import_()
        self.assertIn(steps[0]["key"], str(raised.exception.detail))


class ImportThrottleTest(test.APITestCase):
    def test_imports_are_throttled_per_user(self):
        fixture = fixtures.ProposalFixture()
        self.client.force_authenticate(fixture.staff)
        url = factories.CallFactory.get_protected_list_url("import_call")
        payload = {
            "manager": fixture.manager.uuid.hex,
            "call_data": call_templates.load("rapid_access"),
        }
        rates = {**api_settings.DEFAULT_THROTTLE_RATES, "call_import": "1/min"}
        with mock.patch.object(views.CallImportThrottle, "THROTTLE_RATES", rates):
            first = self.client.post(url, payload, format="json")
            second = self.client.post(url, payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
