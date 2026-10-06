import datetime
from unittest import mock

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import BillingTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.policy import models as policy_models
from waldur_mastermind.policy import policy_actions
from waldur_mastermind.policy import tasks as policy_tasks
from waldur_mastermind.policy.tests import factories as policy_factories

END_DATE = datetime.date(2020, 1, 1)
IN_GRACE = "2020-01-15"


class GracePeriodPauseBaseTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.end_date = END_DATE
        self.project.grace_period_days = 30
        self.project.save()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {"supports_pausing": True}
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

    def pause_by_grace(self):
        self.resource.paused = True
        self.resource.paused_by_grace_period = True
        self.resource.save()

    def extend_end_date(self, end_date=datetime.date(2020, 3, 1)):
        with self.captureOnCommitCallbacks(execute=True):
            self.project.end_date = end_date
            self.project.save()
        self.resource.refresh_from_db()


class GracePeriodPauseTimezoneTest(GracePeriodPauseBaseTest):
    # The end-date task fires at 01:40 in TIME_ZONE, i.e. 23:40 UTC the day
    # before in Central European summer/winter time. The first run after the
    # end date must already see the project in its grace period.
    @override_settings(TIME_ZONE="Europe/Zurich")
    @freeze_time("2020-01-01 23:40:00")
    def test_first_local_run_after_end_date_pauses_resource(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertTrue(self.resource.paused_by_grace_period)

    @override_settings(TIME_ZONE="Europe/Zurich")
    @freeze_time("2020-01-01 22:40:00")
    def test_run_on_local_end_date_does_not_pause_resource(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)

    @override_settings(TIME_ZONE="Europe/Zurich")
    @freeze_time("2020-01-31 23:40:00")
    def test_project_expiry_uses_local_date(self):
        # 2020-02-01 local: the effective end date (2020-01-31) has passed.
        self.assertTrue(self.project.is_expired)
        self.assertFalse(self.project.is_in_grace_period)


@freeze_time(IN_GRACE)
class GracePeriodPauseTaskTest(GracePeriodPauseBaseTest):
    def test_grace_task_marks_its_own_pause(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertTrue(self.resource.paused_by_grace_period)

    def test_grace_task_does_not_claim_existing_pause(self):
        self.resource.paused = True
        self.resource.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_grace_disabled_offering_is_not_held(self):
        self.offering.plugin_options = {
            "supports_pausing": True,
            "disable_grace_period": True,
        }
        self.offering.save()
        self.resource.refresh_from_db()

        self.assertFalse(utils.is_held_by_project_grace(self.resource))


@freeze_time(IN_GRACE)
class GracePeriodPauseReleaseTest(GracePeriodPauseBaseTest):
    def test_extending_end_date_unpauses_resource(self):
        self.pause_by_grace()

        self.extend_end_date()

        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_clearing_end_date_unpauses_resource(self):
        self.pause_by_grace()

        self.extend_end_date(end_date=None)

        self.assertFalse(self.resource.paused)

    def test_extension_within_grace_keeps_resource_paused(self):
        self.pause_by_grace()

        self.extend_end_date(end_date=datetime.date(2020, 1, 10))

        self.assertTrue(self.resource.paused)
        self.assertTrue(self.resource.paused_by_grace_period)

    def test_extension_does_not_lift_unmarked_pause(self):
        self.resource.paused = True
        self.resource.save()

        self.extend_end_date()

        self.assertTrue(self.resource.paused)

    def test_extension_keeps_pause_of_usage_limit_restriction(self):
        self.pause_by_grace()
        self.resource.usage_limit_restriction = "paused"
        self.resource.save()

        self.extend_end_date()

        self.assertTrue(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_extension_keeps_pause_of_firing_cost_policy(self):
        self.pause_by_grace()
        policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, actions="request_pausing", has_fired=True
        )

        self.extend_end_date()

        self.assertTrue(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_extension_keeps_pause_of_slurm_usage_over_grace_limit(self):
        self.pause_by_grace()
        policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
        )

        with mock.patch.object(
            policy_models.SlurmPeriodicUsagePolicy,
            "get_resource_usage_percentage",
            return_value=150,
        ):
            self.extend_end_date()

        self.assertTrue(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_daily_task_releases_end_date_changed_without_signal(self):
        self.pause_by_grace()
        type(self.project).objects.filter(pk=self.project.pk).update(
            end_date=datetime.date(2020, 3, 1)
        )

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_staff_pause_takes_over_from_grace_period(self):
        self.pause_by_grace()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "set_paused"),
            {"paused": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["status"], "Resource paused flag is not changed."
        )
        self.extend_end_date()
        self.assertTrue(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_extension_skips_terminated_resource(self):
        self.pause_by_grace()
        models.Resource.objects.filter(pk=self.resource.pk).update(
            state=ResourceStates.TERMINATED
        )

        self.extend_end_date()

        self.assertTrue(self.resource.paused)

    def test_marker_change_is_not_logged_as_resource_change(self):
        self.pause_by_grace()

        with mock.patch(
            "waldur_mastermind.marketplace.handlers.event_logger"
        ) as event_logger:
            self.resource.paused_by_grace_period = False
            self.resource.save(update_fields=["paused_by_grace_period"])

        event_logger.emit.assert_not_called()


@freeze_time(IN_GRACE)
class GracePeriodPauseGuardTest(GracePeriodPauseBaseTest):
    def create_slurm_policy(self):
        return policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
        )

    def test_slurm_policy_action_keeps_grace_pause(self):
        self.pause_by_grace()
        policy = self.create_slurm_policy()

        with mock.patch.object(
            policy_models.SlurmPeriodicUsagePolicy,
            "get_resource_usage_percentage",
            return_value=10,
        ):
            policy_actions.request_slurm_resource_pausing(policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_slurm_policy_action_does_not_claim_other_pause(self):
        # A pause set by staff (or by the policy itself, for usage) is left in
        # place during the grace period but not handed to it, so extending the
        # end date does not lift it.
        self.resource.paused = True
        self.resource.save()
        policy = self.create_slurm_policy()

        with mock.patch.object(
            policy_models.SlurmPeriodicUsagePolicy,
            "get_resource_usage_percentage",
            return_value=10,
        ):
            policy_actions.request_slurm_resource_pausing(policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)
        self.extend_end_date()
        self.assertTrue(self.resource.paused)

    def test_slurm_policy_action_unpauses_outside_grace_period(self):
        self.project.end_date = None
        self.project.save()
        self.resource.paused = True
        self.resource.save()
        policy = self.create_slurm_policy()

        with mock.patch.object(
            policy_models.SlurmPeriodicUsagePolicy,
            "get_resource_usage_percentage",
            return_value=10,
        ):
            policy_actions.request_slurm_resource_pausing(policy)

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)

    def test_cost_policy_reset_hands_its_own_pause_to_grace_period(self):
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, actions="request_pausing"
        )
        policy_actions.request_pausing(policy)
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

        policy_actions.reset_pausing(policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertTrue(self.resource.paused_by_grace_period)

    def test_cost_policy_reset_keeps_grace_pause(self):
        self.pause_by_grace()
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, actions="request_pausing"
        )

        policy_actions.reset_pausing(policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_usage_limit_lift_keeps_grace_pause(self):
        self.offering.plugin_options = {
            "supports_pausing": True,
            "action_on_usage_limit": "pause",
        }
        self.offering.save()
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            billing_type=BillingTypes.LIMIT,
            limit_amount=100,
        )
        self.resource.paused = True
        self.resource.usage_limit_restriction = "paused"
        self.resource.save()

        utils.evaluate_usage_limit_restriction(self.resource)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        # Kept, so the hourly re-evaluation lifts it after the grace period.
        self.assertEqual(self.resource.usage_limit_restriction, "paused")
        self.assertFalse(self.resource.paused_by_grace_period)


@freeze_time(IN_GRACE)
class SlurmPolicyLiftOwnershipTest(GracePeriodPauseBaseTest):
    """Outside the grace period a SLURM policy under its limit lifts a pause,
    but not one a usage-limit restriction or a firing cost policy holds."""

    def setUp(self):
        super().setUp()
        self.project.end_date = None
        self.project.save()
        self.policy = policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
        )
        self.resource.paused = True
        self.resource.save()

    def evaluate(self):
        with (
            mock.patch.object(
                policy_models.SlurmPeriodicUsagePolicy,
                "get_resource_usage_percentage",
                return_value=10,
            ),
            mock.patch.object(
                policy_models.SlurmPeriodicUsagePolicy,
                "apply_policy_actions",
                return_value=True,
            ),
        ):
            policy_tasks.evaluate_resource_against_policy(
                self.resource.uuid.hex, self.policy.uuid.hex
            )
        self.resource.refresh_from_db()

    def test_lifts_own_pause_and_clears_marker(self):
        self.resource.paused_by_grace_period = True
        self.resource.save()

        self.evaluate()

        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_keeps_usage_limit_pause(self):
        self.resource.usage_limit_restriction = "paused"
        self.resource.save()

        self.evaluate()

        self.assertTrue(self.resource.paused)

    def test_keeps_pause_of_firing_cost_policy(self):
        policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, actions="request_pausing", has_fired=True
        )

        self.evaluate()

        self.assertTrue(self.resource.paused)

    def test_policy_action_keeps_usage_limit_pause(self):
        self.resource.usage_limit_restriction = "paused"
        self.resource.save()

        with mock.patch.object(
            policy_models.SlurmPeriodicUsagePolicy,
            "get_resource_usage_percentage",
            return_value=10,
        ):
            policy_actions.request_slurm_resource_pausing(self.policy)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_cost_policy_of_other_project_does_not_keep_pause(self):
        policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=structure_factories.ProjectFactory(),
            actions="request_pausing",
            has_fired=True,
        )

        self.assertFalse(policy_actions.policies_keep_paused(self.resource))


@freeze_time(IN_GRACE)
class GracePeriodPauseAdoptionTest(GracePeriodPauseBaseTest):
    def adopt(self):
        utils.adopt_grace_period_pauses()
        self.resource.refresh_from_db()

    def test_resource_paused_by_grace_task_is_marked(self):
        utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused_by_grace_period=False
        )

        self.adopt()

        self.assertTrue(self.resource.paused_by_grace_period)

    def test_daily_task_recovers_pause_of_already_extended_project(self):
        utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused_by_grace_period=False
        )
        type(self.project).objects.filter(pk=self.project.pk).update(
            end_date=datetime.date(2020, 3, 1)
        )

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.paused_by_grace_period)

    def test_grace_pause_after_policy_reset_is_marked(self):
        # The flip-flop seen in production: grace pause, SLURM policy reset,
        # grace pause again -- the latest change is a grace-period pause.
        with freeze_time("2020-01-10"):
            utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused=False, paused_by_grace_period=False
        )
        with freeze_time("2020-01-11"):
            self.resource.refresh_from_db()
            utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused_by_grace_period=False
        )

        self.adopt()

        self.assertTrue(self.resource.paused_by_grace_period)

    def test_pause_changed_after_grace_pause_is_not_marked(self):
        with freeze_time("2020-01-10"):
            utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused_by_grace_period=False
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "set_paused")
        self.client.post(url, {"paused": False})
        self.client.post(url, {"paused": True})

        self.adopt()

        self.assertFalse(self.resource.paused_by_grace_period)

    def test_later_change_logged_after_another_field_is_not_marked(self):
        with freeze_time("2020-01-10"):
            utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused_by_grace_period=False
        )
        with freeze_time("2020-01-12"):
            event = logging_models.Event.objects.create(
                event_type="marketplace_resource_update_succeeded",
                message="Marketplace resource 'x' has been changed. Field "
                "'attributes': from {} to {}, field 'paused': from False to True.",
                context={},
            )
        logging_models.Feed.objects.create(event=event, scope=self.resource)

        self.adopt()

        self.assertFalse(self.resource.paused_by_grace_period)

    def test_resource_paused_otherwise_is_not_marked(self):
        self.resource.paused = True
        self.resource.save()

        self.adopt()

        self.assertFalse(self.resource.paused_by_grace_period)

    def test_terminated_resource_is_not_marked(self):
        utils.pause_for_project_grace(self.resource)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            paused_by_grace_period=False, state=ResourceStates.TERMINATED
        )

        self.adopt()

        self.assertFalse(self.resource.paused_by_grace_period)
