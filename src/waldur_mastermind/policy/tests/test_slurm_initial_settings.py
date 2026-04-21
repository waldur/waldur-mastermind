"""Tests for periodic sync of grace-adjusted SLURM limits."""

import json
from unittest import mock

from django.test import TestCase

from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.marketplace.models import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy
from waldur_mastermind.policy.tasks import sync_slurm_periodic_settings


class TestSyncSlurmPeriodicSettings(TestCase):
    """Test the periodic beat job that syncs SLURM limits for all resources."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()

        self.offering = marketplace_factories.OfferingFactory(
            project=self.fixture.offering_project,
            customer=self.fixture.offering_customer,
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="nodeHours", billing_type="usage"
        )

        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.component,
            amount=10,
        )

        self.policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0.3,
            carryover_enabled=False,
            tres_billing_enabled=False,
            limit_type="GrpTRESMins",
        )

        event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[{"object_type": "resource_periodic_limits"}],
        )
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=event_subscription,
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_sends_grace_adjusted_limits(self, mock_publish):
        """Beat job sends apply_periodic_settings with grace-adjusted GrpTRESMins."""
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-grace-account",
            limits={"nodeHours": 1},
            state=ResourceStates.OK,
        )

        result = sync_slurm_periodic_settings()

        self.assertEqual(result["sent"], 1)
        mock_publish.assert_called_once()

        message = mock_publish.call_args[0][0][0]
        payload = json.loads(message["payload"])

        self.assertEqual(payload["action"], "apply_periodic_settings")
        self.assertEqual(payload["backend_id"], "sync-grace-account")

        # 1 node-hour = 60 minutes, with 30% grace = 78 minutes
        self.assertEqual(
            payload["settings"]["grp_tres_mins"]["nodeHours"],
            int(60 * 1.3),
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_sends_settings_for_all_ok_resources(self, mock_publish):
        """Beat job sends apply_periodic_settings for every OK resource."""
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-r1",
            limits={"nodeHours": 10},
            state=ResourceStates.OK,
        )
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-r2",
            limits={"nodeHours": 10},
            state=ResourceStates.OK,
        )

        result = sync_slurm_periodic_settings()

        self.assertEqual(result["sent"], 2)
        self.assertEqual(mock_publish.call_count, 2)

        for call in mock_publish.call_args_list:
            message = call[0][0][0]
            payload = json.loads(message["payload"])
            self.assertEqual(payload["action"], "apply_periodic_settings")
            # 10 node-hours = 600 min, with 30% grace = 780 min
            self.assertEqual(
                payload["settings"]["grp_tres_mins"]["nodeHours"],
                int(600 * 1.3),
            )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_skips_terminated_resources(self, mock_publish):
        """Beat job does not send settings for terminated resources."""
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-terminated",
            limits={"nodeHours": 10},
            state=ResourceStates.TERMINATED,
        )

        result = sync_slurm_periodic_settings()

        self.assertEqual(result["sent"], 0)
        mock_publish.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_skips_creating_resources(self, mock_publish):
        """Beat job does not send settings for resources still being created."""
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-creating",
            limits={"nodeHours": 10},
            state=ResourceStates.CREATING,
        )

        result = sync_slurm_periodic_settings()

        self.assertEqual(result["sent"], 0)
        mock_publish.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_noop_without_policies(self, mock_publish):
        """Beat job is a no-op when no SLURM policies exist."""
        self.policy.delete()

        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-no-policy",
            limits={"nodeHours": 10},
            state=ResourceStates.OK,
        )

        result = sync_slurm_periodic_settings()

        self.assertEqual(result["sent"], 0)
        mock_publish.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_skips_unchanged_settings(self, mock_publish):
        """Second run skips resources whose settings haven't changed."""
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-cached",
            limits={"nodeHours": 10},
            state=ResourceStates.OK,
        )

        # First run: sends settings
        result1 = sync_slurm_periodic_settings()
        self.assertEqual(result1["sent"], 1)
        self.assertEqual(result1["skipped"], 0)

        mock_publish.reset_mock()

        # Second run: skips (settings unchanged)
        result2 = sync_slurm_periodic_settings()
        self.assertEqual(result2["sent"], 0)
        self.assertEqual(result2["skipped"], 1)
        mock_publish.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_resends_after_policy_change(self, mock_publish):
        """Settings are re-sent when policy grace_ratio changes."""
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="sync-policy-change",
            limits={"nodeHours": 10},
            state=ResourceStates.OK,
        )

        # First run
        sync_slurm_periodic_settings()
        mock_publish.reset_mock()

        # Change policy
        self.policy.grace_ratio = 0.5
        self.policy.save()

        # Second run: should re-send because settings hash changed
        result = sync_slurm_periodic_settings()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 0)

        message = mock_publish.call_args[0][0][0]
        payload = json.loads(message["payload"])
        # 10 node-hours = 600 min, with 50% grace = 900 min
        self.assertEqual(
            payload["settings"]["grp_tres_mins"]["nodeHours"],
            int(600 * 1.5),
        )
