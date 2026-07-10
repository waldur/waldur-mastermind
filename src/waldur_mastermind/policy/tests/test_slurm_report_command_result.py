"""Tests for the report-command-result action of SLURM periodic usage policies.

Regression coverage for Sentry issue CSCS-5JE: updating the recent command
history records must use a single bulk UPDATE, not one UPDATE per record.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy import models
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory


class ReportCommandResultTest(test.APITestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.resource = marketplace_factories.ResourceFactory(offering=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(
            self.policy, "report-command-result"
        )
        self.staff = structure_factories.UserFactory(is_staff=True)

    def _create_commands(self, count):
        return [
            models.SlurmCommandHistory.objects.create(
                policy=self.policy,
                resource=self.resource,
                billing_period="2026-Q1",
                command_type="fairshare",
                description="desc",
                shell_command="sacctmgr ...",
                parameters={},
            )
            for _ in range(count)
        ]

    def _count_history_updates(self, queries):
        return sum(
            1 for q in queries if 'UPDATE "policy_slurmcommandhistory"' in q["sql"]
        )

    def test_success_report_updates_all_recent_commands_with_single_update(self):
        commands = self._create_commands(5)
        self.client.force_authenticate(self.staff)

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.post(
                self.url,
                {
                    "resource_uuid": self.resource.uuid.hex,
                    "success": True,
                    "mode": "emulator",
                },
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Single bulk UPDATE regardless of the number of history rows (CSCS-5JE).
        self.assertEqual(self._count_history_updates(ctx.captured_queries), 1)

        for cmd in commands:
            cmd.refresh_from_db()
            self.assertEqual(cmd.execution_mode, "emulator")
            self.assertTrue(cmd.success)

    def test_failure_report_records_error_on_all_commands(self):
        commands = self._create_commands(3)
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            self.url,
            {
                "resource_uuid": self.resource.uuid.hex,
                "success": False,
                "error_message": "boom",
                "mode": "production",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        for cmd in commands:
            cmd.refresh_from_db()
            self.assertFalse(cmd.success)
            self.assertEqual(cmd.error_message, "boom")

    def test_no_history_records_is_handled(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.url,
            {"resource_uuid": self.resource.uuid.hex, "success": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
