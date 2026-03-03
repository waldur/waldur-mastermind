"""Load and performance tests for SLURM periodic usage policy evaluation.

These tests validate that the policy evaluation pipeline (including persistent
logging) handles large-scale deployments correctly. They measure disk space,
computational load, and query performance.

Run with: pytest -s -m slow test_slurm_policy_load.py (use -s for print output)
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models, tasks
from waldur_mastermind.policy.tests import factories


@pytest.mark.slow
class TestSlurmPolicyLargeScale(TransactionTestCase):
    """Large-scale tests for SLURM policy evaluation with 10,000+ resources."""

    RESOURCE_COUNT = 10_000

    def _create_plan_period(self, resource):
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(self, resource, component, usage_amount, plan_period):
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        self.offering = factories.OfferingFactory(type="Marketplace.Slurm")
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)

        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours", name="Node hours"
        )

        # Create policy with progressive QoS
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions=(
                "notify_organization_owners,"
                "request_slurm_resource_downscaling,"
                "request_slurm_resource_pausing"
            ),
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=15,
            limit_type="GrpTRESMins",
            tres_billing_enabled=True,
            qos_strategy="progressive",
            period=3,
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

    def _create_resources_with_usage(self, count, usage_distribution=None):
        """Bulk-create resources with varying usage levels.

        usage_distribution: dict mapping usage_amount to fraction of resources.
        Default exercises all code paths:
            20% below threshold, 30% at notification, 25% at slowdown,
            15% at grace, 10% above grace.
        """
        if usage_distribution is None:
            usage_distribution = {
                500: 0.20,  # Below 80% threshold
                850: 0.30,  # At notification (80-100%)
                1050: 0.25,  # At slowdown (100-120%)
                1150: 0.15,  # At grace limit (~115%)
                1300: 0.10,  # Above grace (130%)
            }

        # Create plan for resources
        plan = marketplace_models.Plan.objects.create(
            offering=self.offering,
            name="Test plan",
        )
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=self.component,
            amount=1000,
            price=1,
        )

        # Bulk create resources
        resources = []
        for i in range(count):
            resources.append(
                marketplace_models.Resource(
                    offering=self.offering,
                    project=self.project,
                    plan=plan,
                    name=f"resource-{i}",
                    backend_id=f"slurm-acct-{i}",
                )
            )
        marketplace_models.Resource.objects.bulk_create(resources)
        created_resources = list(
            marketplace_models.Resource.objects.filter(
                offering=self.offering, name__startswith="resource-"
            ).order_by("id")[:count]
        )

        # Create usage records based on distribution
        idx = 0
        for usage_amount, fraction in usage_distribution.items():
            segment_count = int(count * fraction)
            for r in created_resources[idx : idx + segment_count]:
                plan_period = self._create_plan_period(r)
                self._create_component_usage(
                    r, self.component, usage_amount, plan_period
                )
            idx += segment_count

        return created_resources

    @patch("waldur_mastermind.policy.tasks.notify_about_resource_usage.delay")
    def test_evaluate_all_resources_creates_logs(self, mock_notify):
        """Run evaluation for 10,000 resources and verify log creation."""
        resources = self._create_resources_with_usage(self.RESOURCE_COUNT)

        start = time.time()
        for resource in resources:
            tasks.evaluate_resource_against_policy(
                str(resource.uuid), str(self.policy.uuid)
            )
        elapsed = time.time() - start

        log_count = models.SlurmPolicyEvaluationLog.objects.filter(
            policy=self.policy
        ).count()

        print("\n=== Evaluation Performance ===")
        print(f"Resources evaluated: {self.RESOURCE_COUNT}")
        print(f"Evaluation logs created: {log_count}")
        print(f"Total time: {elapsed:.2f}s")
        print(f"Records per second: {log_count / elapsed:.1f}")

        self.assertEqual(log_count, self.RESOURCE_COUNT)

    def test_evaluation_log_disk_usage(self):
        """Measure DB size for evaluation logs across 4 quarterly periods."""
        resources = self._create_resources_with_usage(self.RESOURCE_COUNT)
        billing_periods = [
            timezone.now().replace(month=m, day=1).date() for m in [1, 4, 7, 10]
        ]

        total_records = 0
        for period in billing_periods:
            logs = []
            for resource in resources:
                logs.append(
                    models.SlurmPolicyEvaluationLog(
                        policy=self.policy,
                        resource=resource,
                        billing_period=period,
                        usage_percentage=75.0,
                        grace_limit_percentage=120.0,
                        actions_taken=["notify"],
                        previous_state={"paused": False, "downscaled": False},
                        new_state={"paused": False, "downscaled": False},
                        stomp_message_sent=False,
                    )
                )
            models.SlurmPolicyEvaluationLog.objects.bulk_create(logs, batch_size=5000)
            total_records += len(logs)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_total_relation_size('policy_slurmpolicyevaluationlog')"
            )
            total_bytes = cursor.fetchone()[0]

        bytes_per_record = total_bytes / total_records if total_records else 0

        print("\n=== Evaluation Log Disk Usage ===")
        print(f"Total records: {total_records}")
        print(f"Total DB size: {total_bytes / 1024 / 1024:.2f} MB")
        print(f"Bytes per record: {bytes_per_record:.0f}")
        print(
            f"Projected annual (weekly evals, 10K resources): "
            f"{bytes_per_record * 52 * self.RESOURCE_COUNT / 1024 / 1024:.0f} MB"
        )

        self.assertEqual(total_records, self.RESOURCE_COUNT * 4)

    def test_command_history_disk_usage(self):
        """Measure DB size for command history records."""
        resources = self._create_resources_with_usage(self.RESOURCE_COUNT)
        period = timezone.now().replace(day=1).date()

        command_types = ["fairshare", "limits", "qos", "reset_usage"]
        logs = []
        for resource in resources:
            for cmd_type in command_types:
                logs.append(
                    models.SlurmCommandHistory(
                        policy=self.policy,
                        resource=resource,
                        billing_period=period,
                        command_type=cmd_type,
                        description=f"Set {cmd_type} for {resource.name}",
                        shell_command=f"sacctmgr --immediate modify account {resource.backend_id} set {cmd_type}=value",
                        parameters={"account": resource.backend_id, "value": "100"},
                        execution_mode="production",
                    )
                )
        models.SlurmCommandHistory.objects.bulk_create(logs, batch_size=5000)

        total_records = len(logs)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_total_relation_size('policy_slurmcommandhistory')"
            )
            total_bytes = cursor.fetchone()[0]

        bytes_per_record = total_bytes / total_records if total_records else 0

        print("\n=== Command History Disk Usage ===")
        print(f"Total records: {total_records}")
        print(f"Total DB size: {total_bytes / 1024 / 1024:.2f} MB")
        print(f"Bytes per record: {bytes_per_record:.0f}")

        self.assertEqual(total_records, self.RESOURCE_COUNT * 4)

    @patch("waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay")
    def test_fanout_task_performance(self, mock_delay):
        """Measure how long it takes to fan out to 10,000 resources."""
        self._create_resources_with_usage(self.RESOURCE_COUNT)

        # Create a usage record to trigger evaluation
        resource = marketplace_models.Resource.objects.filter(
            offering=self.offering
        ).first()
        plan_period = self._create_plan_period(resource)
        usage = self._create_component_usage(resource, self.component, 500, plan_period)

        start = time.time()
        tasks.evaluate_slurm_resource_policy(str(resource.uuid), str(usage.uuid))
        elapsed = time.time() - start

        fanout_count = mock_delay.call_count

        print("\n=== Task Fanout Performance ===")
        print(f"Fanout time: {elapsed:.2f}s")
        print(f"Tasks queued: {fanout_count}")
        print(
            f"Tasks per second: {fanout_count / elapsed:.0f}" if elapsed > 0 else "N/A"
        )

        # The fanout should queue at least 1 evaluation task
        self.assertGreaterEqual(fanout_count, 1)

    @patch("waldur_mastermind.policy.tasks.notify_about_resource_usage.delay")
    def test_evaluate_with_complex_policy(self, mock_notify):
        """Test evaluation with progressive QoS, carryover, and multiple TRES types."""
        # Add more TRES billing weights
        self.policy.tres_billing_weights = {"cpu": 1.0, "mem": 0.25, "gres/gpu": 2.5}
        self.policy.save()

        # Add more component limits
        comp2 = factories.OfferingComponentFactory(
            offering=self.offering, type="gpu-hours", name="GPU hours"
        )
        comp3 = factories.OfferingComponentFactory(
            offering=self.offering, type="mem-hours", name="Memory hours"
        )
        models.OfferingComponentLimit.objects.create(
            policy=self.policy, component=comp2, limit=500
        )
        models.OfferingComponentLimit.objects.create(
            policy=self.policy, component=comp3, limit=2000
        )

        resources = self._create_resources_with_usage(self.RESOURCE_COUNT)

        start = time.time()
        evaluated = 0
        for resource in resources:
            tasks.evaluate_resource_against_policy(
                str(resource.uuid), str(self.policy.uuid)
            )
            evaluated += 1
        elapsed = time.time() - start

        log_count = models.SlurmPolicyEvaluationLog.objects.filter(
            policy=self.policy
        ).count()

        print("\n=== Complex Policy Evaluation ===")
        print(f"Resources evaluated: {evaluated}")
        print(f"Logs created: {log_count}")
        print(f"Total time: {elapsed:.2f}s")
        print(f"Records per second: {log_count / elapsed:.1f}")

        self.assertEqual(log_count, self.RESOURCE_COUNT)

    @patch("waldur_mastermind.policy.tasks.notify_about_resource_usage.delay")
    def test_concurrent_evaluations(self, mock_notify):
        """Test concurrent evaluation calls for race conditions."""
        resources = self._create_resources_with_usage(min(100, self.RESOURCE_COUNT))

        def evaluate_resource(resource):
            tasks.evaluate_resource_against_policy(
                str(resource.uuid), str(self.policy.uuid)
            )
            return resource.uuid

        start = time.time()
        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(evaluate_resource, r): r for r in resources[:100]
            }
            for future in as_completed(futures):
                results.append(future.result())
        elapsed = time.time() - start

        log_count = models.SlurmPolicyEvaluationLog.objects.filter(
            policy=self.policy
        ).count()

        # Check for duplicates
        unique_logs = (
            models.SlurmPolicyEvaluationLog.objects.filter(policy=self.policy)
            .values_list("resource_id", flat=True)
            .distinct()
            .count()
        )

        print("\n=== Concurrent Evaluation Stress Test ===")
        print(f"Concurrent evaluations: {len(results)}")
        print(f"Logs created: {log_count}")
        print(f"Unique resource logs: {unique_logs}")
        print(f"Total time: {elapsed:.2f}s")

        # Each resource should have exactly one evaluation log
        self.assertEqual(log_count, len(results))
        self.assertEqual(unique_logs, len(results))

    def test_log_retention_growth(self):
        """Simulate 12 months of weekly evaluations to measure annual disk usage."""
        # Use smaller subset for retention test
        resource_count = min(1000, self.RESOURCE_COUNT)
        resources = self._create_resources_with_usage(resource_count)

        # Simulate 52 weeks of evaluations
        weeks = 52
        logs = []
        base_date = timezone.now().replace(day=1)
        for week in range(weeks):
            period = (base_date - timezone.timedelta(weeks=week)).date().replace(day=1)
            for resource in resources:
                logs.append(
                    models.SlurmPolicyEvaluationLog(
                        policy=self.policy,
                        resource=resource,
                        billing_period=period,
                        usage_percentage=75.0 + (week % 30),
                        grace_limit_percentage=120.0,
                        actions_taken=["notify"] if week % 3 == 0 else [],
                        previous_state={"paused": False, "downscaled": False},
                        new_state={"paused": False, "downscaled": False},
                        stomp_message_sent=week % 5 == 0,
                    )
                )
        models.SlurmPolicyEvaluationLog.objects.bulk_create(logs, batch_size=10000)

        total_records = len(logs)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_total_relation_size('policy_slurmpolicyevaluationlog')"
            )
            total_bytes = cursor.fetchone()[0]

        bytes_per_record = total_bytes / total_records if total_records else 0

        print("\n=== Log Retention Growth ===")
        print(f"Resources: {resource_count}")
        print(f"Weeks simulated: {weeks}")
        print(f"Total records: {total_records}")
        print(f"Total DB size: {total_bytes / 1024 / 1024:.2f} MB")
        print(f"Bytes per record: {bytes_per_record:.0f}")
        print(
            f"Projected for 10K resources, 1 year: "
            f"{bytes_per_record * weeks * self.RESOURCE_COUNT / 1024 / 1024:.0f} MB"
        )

        self.assertEqual(total_records, resource_count * weeks)

    def test_evaluation_logs_api_query_performance(self):
        """Measure API query response time with 100K evaluation log entries."""
        resource_count = min(1000, self.RESOURCE_COUNT)
        resources = self._create_resources_with_usage(resource_count)

        # Create 100 logs per resource
        logs = []
        for resource in resources:
            for i in range(100):
                period = timezone.now().replace(day=1).date()
                logs.append(
                    models.SlurmPolicyEvaluationLog(
                        policy=self.policy,
                        resource=resource,
                        billing_period=period,
                        usage_percentage=50.0 + i,
                        grace_limit_percentage=120.0,
                        actions_taken=[],
                        previous_state={"paused": False, "downscaled": False},
                        new_state={"paused": False, "downscaled": False},
                    )
                )
        models.SlurmPolicyEvaluationLog.objects.bulk_create(logs, batch_size=10000)

        total = models.SlurmPolicyEvaluationLog.objects.count()
        print("\n=== Evaluation Logs Query Performance ===")
        print(f"Total records: {total}")

        # Filtered by resource
        target_resource = resources[0]
        start = time.time()
        result = list(
            models.SlurmPolicyEvaluationLog.objects.filter(
                policy=self.policy,
                resource=target_resource,
            ).order_by("-evaluated_at")[:100]
        )
        filtered_time = time.time() - start
        print(
            f"Filtered by resource: {filtered_time * 1000:.1f}ms ({len(result)} results)"
        )

        # Filtered by billing period
        period = timezone.now().replace(day=1).date()
        start = time.time()
        result = list(
            models.SlurmPolicyEvaluationLog.objects.filter(
                policy=self.policy,
                billing_period=period,
            ).order_by("-evaluated_at")[:100]
        )
        period_time = time.time() - start
        print(f"Filtered by period: {period_time * 1000:.1f}ms ({len(result)} results)")

        # Unfiltered (policy only)
        start = time.time()
        result = list(
            models.SlurmPolicyEvaluationLog.objects.filter(
                policy=self.policy,
            ).order_by("-evaluated_at")[:100]
        )
        unfiltered_time = time.time() - start
        print(f"Unfiltered: {unfiltered_time * 1000:.1f}ms ({len(result)} results)")

        # Assert queries complete within 500ms
        self.assertLess(filtered_time, 0.5)
        self.assertLess(period_time, 0.5)
        self.assertLess(unfiltered_time, 0.5)

    def test_command_history_api_query_performance(self):
        """Measure command history query response time."""
        resource_count = min(1000, self.RESOURCE_COUNT)
        resources = self._create_resources_with_usage(resource_count)
        period = timezone.now().replace(day=1).date()

        logs = []
        for resource in resources:
            for cmd_type in ["fairshare", "limits", "qos", "reset_usage"]:
                logs.append(
                    models.SlurmCommandHistory(
                        policy=self.policy,
                        resource=resource,
                        billing_period=period,
                        command_type=cmd_type,
                        description=f"Set {cmd_type}",
                        shell_command=f"sacctmgr modify account {resource.backend_id}",
                        parameters={"value": "100"},
                    )
                )
        models.SlurmCommandHistory.objects.bulk_create(logs, batch_size=10000)

        total = models.SlurmCommandHistory.objects.count()
        print("\n=== Command History Query Performance ===")
        print(f"Total records: {total}")

        target_resource = resources[0]
        start = time.time()
        result = list(
            models.SlurmCommandHistory.objects.filter(
                policy=self.policy,
                resource=target_resource,
            ).order_by("-executed_at")[:100]
        )
        filtered_time = time.time() - start
        print(
            f"Filtered by resource: {filtered_time * 1000:.1f}ms ({len(result)} results)"
        )

        start = time.time()
        result = list(
            models.SlurmCommandHistory.objects.filter(
                policy=self.policy,
            ).order_by("-executed_at")[:100]
        )
        unfiltered_time = time.time() - start
        print(f"Unfiltered: {unfiltered_time * 1000:.1f}ms ({len(result)} results)")

        self.assertLess(filtered_time, 0.5)
        self.assertLess(unfiltered_time, 0.5)
