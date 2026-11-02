"""Editing a fired cost policy brings its resources in line with the edit (#940).

A policy fired before the save and still triggered after it crosses no limit,
so neither the fire nor the reset path runs. An added action used to wait for
the 4-hourly sweep, and a removed one was never reset: a resource paused by a
policy whose action had since become downscaling stayed paused, with no policy
owning the pause, and was not downscaled.
"""

import dataclasses
from unittest import mock

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import utils
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.policy_actions import POLICY_ACTIONS
from waldur_mastermind.policy.tests import factories as policy_factories

TOTAL = ProjectEstimatedCostPolicy.Periods.TOTAL


def mock_action(name):
    """Replace the method that policies call for action ``name``.

    Policies call `POLICY_ACTIONS[name].method`, which holds the function
    itself, so patching the module attribute would intercept nothing.
    """
    method = mock.Mock(__name__=name)
    return method, mock.patch.dict(
        POLICY_ACTIONS,
        {name: dataclasses.replace(POLICY_ACTIONS[name], method=method)},
    )


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=0)
@freeze_time("2026-07-15")
class FiredPolicyActionEditTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self._enable_actions(self.resource.offering)
        self.other_offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer
        )
        self._enable_actions(self.other_offering)
        self.other_resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.other_offering,
            state=ResourceStates.OK,
        )
        self.other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        self.other_project_resource = marketplace_factories.ResourceFactory(
            project=self.other_project,
            offering=self.other_offering,
            state=ResourceStates.OK,
        )
        self.invoice, _ = invoices_models.Invoice.objects.get_or_create(
            customer=self.fixture.customer, month=7, year=2026
        )
        self.invoice.tax_percent = 0
        self.invoice.save()
        invoices_models.InvoiceItem.objects.filter(invoice=self.invoice).delete()
        # Over every limit below, with no credit.
        self._add_cost(self.project, self.resource)
        self.client.force_authenticate(self.fixture.staff)

    def _enable_actions(self, offering):
        offering.plugin_options = {
            **(offering.plugin_options or {}),
            "supports_pausing": True,
            "supports_downscaling": True,
        }
        offering.save()

    def _add_cost(self, project, resource):
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=project,
            resource=resource,
            unit_price=500,
            quantity=1,
        )

    def _fired_policy(self, actions, limit_cost=100, resource=None):
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            resource=resource,
            use_credit=False,
            limit_cost=limit_cost,
            period=TOTAL,
            actions=actions,
        )
        utils.evaluate_policies([policy])
        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self._refresh()
        return policy

    def _refresh(self):
        for resource in (
            self.resource,
            self.other_resource,
            self.other_project_resource,
        ):
            resource.refresh_from_db()

    def _patch(self, policy, **data):
        response = self.client.patch(
            policy_factories.ProjectEstimatedCostPolicyFactory.get_url(policy), data
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._refresh()
        return response

    def _events_after(self, last_event, *event_types):
        return logging_models.Event.objects.filter(
            id__gt=last_event.id, event_type__in=event_types
        )

    def test_changing_pausing_to_downscaling_unpauses_and_downscales(self):
        policy = self._fired_policy("request_pausing")
        self.assertTrue(self.resource.paused)

        self._patch(policy, actions="request_downscaling")

        self.assertFalse(self.resource.paused)
        self.assertTrue(self.resource.downscaled)

    def test_a_removed_action_stays_while_another_firing_policy_wants_it(self):
        policy = self._fired_policy("request_pausing")
        self._fired_policy("request_pausing", limit_cost=200)

        self._patch(policy, actions="request_downscaling")

        # The second policy still fires with request_pausing.
        self.assertTrue(self.resource.paused)
        self.assertTrue(self.resource.downscaled)

    def test_unchanged_actions_are_not_run_again(self):
        policy = self._fired_policy("request_pausing")
        last_event = logging_models.Event.objects.order_by("-id").first()

        request_pausing, patch = mock_action("request_pausing")
        with patch:
            self._patch(policy, limit_cost=150)

        request_pausing.assert_not_called()
        self.assertTrue(self.resource.paused)
        self.assertFalse(
            self._events_after(last_event, "request_pausing", "reset_pausing").exists()
        )

    def test_moving_a_resource_scoped_policy_moves_its_action(self):
        policy = self._fired_policy("request_pausing", resource=self.resource)
        # The new resource's own cost keeps the moved policy over its limit.
        self._add_cost(self.project, self.other_resource)
        self.assertTrue(self.resource.paused)
        self.assertFalse(self.other_resource.paused)

        self._patch(policy, resource=self.other_resource.uuid.hex)

        self.assertFalse(self.resource.paused)
        self.assertTrue(self.other_resource.paused)

    def test_widening_a_resource_scoped_policy_leaves_its_resource_paused(self):
        policy = self._fired_policy("request_pausing", resource=self.resource)
        last_event = logging_models.Event.objects.order_by("-id").first()

        self._patch(policy, resource="")

        self.assertTrue(self.resource.paused)
        self.assertTrue(self.other_resource.paused)
        # Not unpaused and paused again on the way.
        self.assertFalse(self._events_after(last_event, "reset_pausing").exists())

    def test_moving_a_policy_to_another_project_moves_its_action(self):
        policy = self._fired_policy("request_pausing")
        self._add_cost(self.other_project, self.other_project_resource)
        self.assertTrue(self.resource.paused)
        self.assertFalse(self.other_project_resource.paused)

        self._patch(
            policy, scope=structure_factories.ProjectFactory.get_url(self.other_project)
        )

        self.assertFalse(self.resource.paused)
        self.assertFalse(self.other_resource.paused)
        self.assertTrue(self.other_project_resource.paused)

    def test_an_edit_that_also_ends_the_firing_resets_what_was_applied(self):
        policy = self._fired_policy("request_pausing")

        self._patch(policy, actions="request_downscaling", limit_cost=100000)

        policy.refresh_from_db()
        self.assertFalse(policy.has_fired)
        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.downscaled)

    def test_an_added_one_shot_action_waits_for_the_next_fire(self):
        policy = self._fired_policy("request_pausing")

        notify, patch = mock_action("notify_project_team")
        with patch:
            self._patch(policy, actions="request_pausing,notify_project_team")

        notify.assert_not_called()

    def test_a_policy_fired_since_it_was_loaded_is_not_fired_again(self):
        policy = self._fired_policy("request_pausing")
        # The request loaded the policy before an evaluation fired it.
        stale = ProjectEstimatedCostPolicy.objects.get(pk=policy.pk)
        stale.has_fired = False

        request_pausing, patch = mock_action("request_pausing")
        with (
            patch,
            mock.patch(
                "waldur_mastermind.policy.views.ProjectEstimatedCostPolicyViewSet.get_object",
                return_value=stale,
            ),
        ):
            self._patch(policy, limit_cost=150)

        request_pausing.assert_not_called()

    def test_a_policy_fired_since_it_was_loaded_keeps_its_fired_datetime(self):
        policy = self._fired_policy("request_pausing")
        fired_datetime = policy.fired_datetime
        self.assertIsNotNone(fired_datetime)
        # The request loaded the policy before an evaluation fired it.
        stale = ProjectEstimatedCostPolicy.objects.get(pk=policy.pk)
        stale.has_fired = False
        stale.fired_datetime = None

        with mock.patch(
            "waldur_mastermind.policy.views.ProjectEstimatedCostPolicyViewSet.get_object",
            return_value=stale,
        ):
            self._patch(policy, limit_cost=150)

        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self.assertEqual(policy.fired_datetime, fired_datetime)

    def test_a_policy_that_is_not_fired_behaves_as_before(self):
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            use_credit=False,
            limit_cost=10000,
            period=TOTAL,
            actions="request_pausing",
        )

        self._patch(policy, actions="request_downscaling")

        policy.refresh_from_db()
        self.assertFalse(policy.has_fired)
        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.downscaled)
