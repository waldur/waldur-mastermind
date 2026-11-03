"""SLURM usage policies share ``downscaled`` and ``paused`` with other policies.

``paused`` was already shared: a SLURM policy lifting its pause asks the pause
keepers first. ``downscaled`` was not -- a SLURM policy under 100 % lifted a
downscale a firing cost policy still wanted, the cost-policy sweep downscaled it
again, and the flag flipped until the cost policy reset. A cost policy's reset
in turn lifted a downscale a SLURM policy over 100 % still wanted.

The SLURM actions have no reset method: they lift their flag on their next run
under the limit. Removed from the policy, or with the policy deleted, they never
run again, so what they held stayed set with no policy owning it.
"""

from unittest import mock

from django.test import TestCase
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import models, policy_actions, tasks
from waldur_mastermind.policy.tests import factories as policy_factories

DOWNSCALE = "request_slurm_resource_downscaling"
PAUSE = "request_slurm_resource_pausing"


class SlurmFlagTestMixin:
    """A resource on an offering that pauses and downscales, with SLURM usage per policy."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.offering = self.fixture.offering
        self.offering.plugin_options = {
            **(self.offering.plugin_options or {}),
            "supports_pausing": True,
            "supports_downscaling": True,
        }
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Usage percentage of the resource under each SLURM policy, by policy
        # uuid: the uuid survives the delete, the pk does not.
        self.usage = {}
        patcher = mock.patch.object(
            models.SlurmPeriodicUsagePolicy,
            "get_resource_usage_percentage",
            autospec=True,
            side_effect=lambda policy, resource, current_period=None: self.usage.get(
                policy.uuid.hex, 0
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The site agent is not under test.
        patcher = mock.patch.object(
            models.SlurmPeriodicUsagePolicy, "apply_policy_actions", return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def slurm_policy(self, actions, usage):
        policy = policy_factories.SlurmPeriodicUsagePolicyFactory(
            scope=self.offering, actions=actions, grace_ratio=0.2
        )
        self.usage[policy.uuid.hex] = usage
        return policy

    def firing_cost_policy(self, actions="request_downscaling"):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, actions=actions, has_fired=True
        )

    def set_flags(self, **flags):
        for name, value in flags.items():
            setattr(self.resource, name, value)
        self.resource.save(update_fields=list(flags))

    def assert_flag(self, name, expected):
        self.resource.refresh_from_db()
        self.assertEqual(getattr(self.resource, name), expected)


class SlurmDownscaleClearTest(SlurmFlagTestMixin, TestCase):
    """A SLURM policy under 100 % leaves a downscale another reason still wants."""

    def evaluate(self, policy):
        tasks.evaluate_resource_against_policy(self.resource.uuid.hex, policy.uuid.hex)

    def test_evaluation_keeps_downscale_while_cost_policy_fires(self):
        self.firing_cost_policy()
        policy = self.slurm_policy(DOWNSCALE, usage=50)
        self.set_flags(downscaled=True)

        self.evaluate(policy)

        self.assert_flag("downscaled", True)

    def test_action_keeps_downscale_while_cost_policy_fires(self):
        self.firing_cost_policy()
        policy = self.slurm_policy(DOWNSCALE, usage=50)
        self.set_flags(downscaled=True)

        policy_actions.request_slurm_resource_downscaling(policy)

        self.assert_flag("downscaled", True)

    def test_evaluation_keeps_downscale_held_by_usage_limit_restriction(self):
        policy = self.slurm_policy(DOWNSCALE, usage=50)
        self.set_flags(downscaled=True, usage_limit_restriction="downscaled")

        self.evaluate(policy)

        self.assert_flag("downscaled", True)

    def test_evaluation_lifts_downscale_when_nothing_else_wants_it(self):
        # A cost policy that has reset no longer holds the flag.
        cost_policy = self.firing_cost_policy()
        cost_policy.has_fired = False
        cost_policy.save(update_fields=["has_fired"])
        policy = self.slurm_policy(DOWNSCALE, usage=50)
        self.set_flags(downscaled=True)

        self.evaluate(policy)

        self.assert_flag("downscaled", False)

    def test_action_lifts_downscale_when_nothing_else_wants_it(self):
        policy = self.slurm_policy(DOWNSCALE, usage=50)
        self.set_flags(downscaled=True)

        policy_actions.request_slurm_resource_downscaling(policy)

        self.assert_flag("downscaled", False)


class CostResetRespectsSlurmTest(SlurmFlagTestMixin, TestCase):
    """A cost policy's reset leaves a flag a SLURM policy or a usage limit holds."""

    def test_reset_keeps_downscale_held_by_usage_limit_restriction(self):
        cost_policy = self.firing_cost_policy()
        self.set_flags(downscaled=True, usage_limit_restriction="downscaled")

        policy_actions.reset_downscaling(cost_policy)

        self.assert_flag("downscaled", True)

    def test_reset_pausing_keeps_pause_while_slurm_policy_is_over_grace(self):
        cost_policy = self.firing_cost_policy(actions="request_pausing")
        self.slurm_policy(PAUSE, usage=125)
        self.set_flags(paused=True)

        policy_actions.reset_pausing(cost_policy)

        self.assert_flag("paused", True)

    def test_reset_pausing_ignores_slurm_policy_under_grace(self):
        cost_policy = self.firing_cost_policy(actions="request_pausing")
        self.slurm_policy(PAUSE, usage=110)
        self.set_flags(paused=True)

        policy_actions.reset_pausing(cost_policy)

        self.assert_flag("paused", False)

    def test_reset_pausing_keeps_pause_held_by_usage_limit_restriction(self):
        cost_policy = self.firing_cost_policy(actions="request_pausing")
        self.set_flags(paused=True, usage_limit_restriction="paused")

        policy_actions.reset_pausing(cost_policy)

        self.assert_flag("paused", True)

    def test_reset_keeps_downscale_while_slurm_policy_is_over(self):
        cost_policy = self.firing_cost_policy()
        self.slurm_policy(DOWNSCALE, usage=105)
        self.set_flags(downscaled=True)

        policy_actions.reset_downscaling(cost_policy)

        self.assert_flag("downscaled", True)

    def test_reset_ignores_slurm_policy_under_limit(self):
        cost_policy = self.firing_cost_policy()
        self.slurm_policy(DOWNSCALE, usage=50)
        self.set_flags(downscaled=True)

        policy_actions.reset_downscaling(cost_policy)

        self.assert_flag("downscaled", False)

    def test_reset_ignores_slurm_policy_without_downscaling(self):
        cost_policy = self.firing_cost_policy()
        self.slurm_policy(PAUSE, usage=105)
        self.set_flags(downscaled=True)

        policy_actions.reset_downscaling(cost_policy)

        self.assert_flag("downscaled", False)


class RemovedSlurmActionTest(SlurmFlagTestMixin, test.APITestCase):
    """Removing a SLURM action, or deleting its policy, lifts what it held."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)

    def edit(self, policy, data):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                policy_factories.SlurmPeriodicUsagePolicyFactory.get_url(policy), data
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def edit_actions(self, policy, actions):
        self.edit(policy, {"actions": actions})

    def delete(self, policy):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(
                policy_factories.SlurmPeriodicUsagePolicyFactory.get_url(policy)
            )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_removing_downscaling_lifts_downscale(self):
        policy = self.slurm_policy(f"{DOWNSCALE},notify_organization_owners", 105)
        self.set_flags(downscaled=True)

        self.edit_actions(policy, "notify_organization_owners")

        self.assert_flag("downscaled", False)

    def test_removing_pausing_lifts_pause(self):
        policy = self.slurm_policy(f"{PAUSE},notify_organization_owners", 125)
        self.set_flags(paused=True)

        self.edit_actions(policy, "notify_organization_owners")

        self.assert_flag("paused", False)

    def test_kept_action_is_not_lifted(self):
        policy = self.slurm_policy(f"{DOWNSCALE},{PAUSE}", 105)
        self.set_flags(downscaled=True)

        self.edit_actions(policy, DOWNSCALE)

        self.assert_flag("downscaled", True)

    def test_moving_policy_lifts_flags_on_old_offering_only(self):
        # Under the limit, the moved policy does not hold the new offering's
        # downscale either, so a lift reaching it would show.
        policy = self.slurm_policy(f"{DOWNSCALE},{PAUSE}", 50)
        self.set_flags(downscaled=True, paused=True)
        new_offering = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            plugin_options={"supports_pausing": True, "supports_downscaling": True},
        )
        other = marketplace_factories.ResourceFactory(
            offering=new_offering, project=self.project, state=ResourceStates.OK
        )
        other.downscaled = True
        other.save(update_fields=["downscaled"])

        self.edit(
            policy,
            {"scope": marketplace_factories.OfferingFactory.get_url(new_offering)},
        )

        policy.refresh_from_db()
        self.assertEqual(policy.scope, new_offering)
        self.assert_flag("downscaled", False)
        self.assert_flag("paused", False)
        other.refresh_from_db()
        self.assertTrue(other.downscaled)

    def test_removing_downscaling_keeps_downscale_a_cost_policy_wants(self):
        self.firing_cost_policy()
        policy = self.slurm_policy(f"{DOWNSCALE},notify_organization_owners", 105)
        self.set_flags(downscaled=True)

        self.edit_actions(policy, "notify_organization_owners")

        self.assert_flag("downscaled", True)

    def test_removing_pausing_keeps_pause_a_cost_policy_wants(self):
        self.firing_cost_policy(actions="request_pausing")
        policy = self.slurm_policy(f"{PAUSE},notify_organization_owners", 125)
        self.set_flags(paused=True)

        self.edit_actions(policy, "notify_organization_owners")

        self.assert_flag("paused", True)

    def test_deleting_policy_lifts_flags_after_usage_dropped(self):
        # A new period or a raised allocation dropped usage before any
        # evaluation lifted the flags; a deleted policy gets no evaluation.
        policy = self.slurm_policy(f"{DOWNSCALE},{PAUSE}", 5)
        self.set_flags(downscaled=True, paused=True)

        self.delete(policy)

        self.assert_flag("downscaled", False)
        self.assert_flag("paused", False)

    def test_pause_held_by_keeper_is_not_handed_to_grace_period(self):
        self.firing_cost_policy(actions="request_pausing")
        policy = self.slurm_policy(PAUSE, usage=125)
        self.set_flags(paused=True)

        with mock.patch(
            "waldur_mastermind.marketplace.utils.is_held_by_project_grace",
            return_value=True,
        ):
            self.delete(policy)

        self.assert_flag("paused", True)
        self.assert_flag("paused_by_grace_period", False)

    def test_pause_only_grace_period_wants_is_handed_to_it(self):
        policy = self.slurm_policy(PAUSE, usage=125)
        self.set_flags(paused=True)

        with mock.patch(
            "waldur_mastermind.marketplace.utils.is_held_by_project_grace",
            return_value=True,
        ):
            self.delete(policy)

        self.assert_flag("paused", True)
        self.assert_flag("paused_by_grace_period", True)

    def test_failed_lift_does_not_fail_the_saved_edit(self):
        policy = self.slurm_policy(f"{DOWNSCALE},notify_organization_owners", 105)
        self.set_flags(downscaled=True)

        with mock.patch(
            "waldur_mastermind.policy.serializers.lift_slurm_actions",
            side_effect=RuntimeError("boom"),
        ):
            self.edit_actions(policy, "notify_organization_owners")

        policy.refresh_from_db()
        self.assertEqual(policy.actions, "notify_organization_owners")

    def test_deleting_policy_lifts_pause_and_downscale(self):
        policy = self.slurm_policy(f"{DOWNSCALE},{PAUSE}", 125)
        self.set_flags(downscaled=True, paused=True)

        self.delete(policy)

        self.assert_flag("downscaled", False)
        self.assert_flag("paused", False)

    def test_deleting_policy_keeps_downscale_a_cost_policy_wants(self):
        self.firing_cost_policy()
        policy = self.slurm_policy(DOWNSCALE, usage=105)
        self.set_flags(downscaled=True)

        self.delete(policy)

        self.assert_flag("downscaled", True)
