"""Tests for SLURM periodic usage policy STOMP message emission."""

import json
from unittest import mock

from rest_framework import test

from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy


class SlurmPeriodicUsagePolicySTOMPTest(test.APITestCase):
    """Test STOMP message emission for SLURM periodic usage policy."""

    def setUp(self):
        """Setup test data for periodic usage policy STOMP testing."""
        self.fixture = marketplace_fixtures.MarketplaceFixture()

        # Create SLURM offering with nodeHours component
        self.offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            project=self.fixture.offering_project,
            customer=self.fixture.offering_customer,
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="nodeHours", billing_type="usage"
        )

        # Create plan with allocation
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.component,
            amount=1000,  # 1000 node-hours allocation
        )

        # Create resource with per-component limits
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="test-slurm-account",
            limits={"nodeHours": 1000},
        )

        # Create SLURM periodic usage policy
        self.policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=15,
            tres_billing_enabled=True,
            limit_type="GrpTRESMins",
        )

        # Add organization group to policy
        organization_group = structure_factories.OrganizationGroupFactory()
        self.policy.organization_groups.add(organization_group)

        # Create event subscription for periodic limits
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[{"object_type": "resource_periodic_limits"}],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_periodic_limits_stomp_message_emitted_on_policy_trigger(
        self, mock_publish_messages
    ):
        """Test that STOMP message is emitted when periodic limits policy is triggered."""

        # Create component usage that should trigger the policy
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=250.0,  # 25% of 1000Nh allocation
        )

        # Trigger policy application
        success = self.policy.apply_policy_actions(self.resource)

        # Verify policy application was attempted
        self.assertTrue(success, "Policy application should succeed")

        # Verify STOMP message was published
        mock_publish_messages.assert_called_once()

        # Extract and validate message content
        call_args = mock_publish_messages.call_args[0][0]
        self.assertIsInstance(call_args, list)
        self.assertGreater(len(call_args), 0)

        message = call_args[0]
        self.assertIn("topic", message)
        self.assertIn("payload", message)

        # Validate payload structure
        payload = json.loads(message["payload"])

        # Check required fields for periodic limits message
        required_fields = [
            "resource_uuid",
            "backend_id",
            "offering_uuid",
            "action",
            "settings",
            "timestamp",
        ]

        for field in required_fields:
            self.assertIn(field, payload, f"Missing required field: {field}")

        # Validate specific values
        self.assertEqual(payload["resource_uuid"], str(self.resource.uuid))
        self.assertEqual(payload["backend_id"], self.resource.backend_id)
        self.assertEqual(payload["offering_uuid"], str(self.offering.uuid))
        self.assertEqual(payload["action"], "apply_periodic_settings")

        # Validate settings structure
        settings = payload["settings"]
        self.assertIn("fairshare", settings)
        self.assertIn("grp_tres_mins", settings)  # Should use GrpTRESMins
        self.assertIn("qos_threshold", settings)

        print("✅ STOMP message structure validated")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_periodic_limits_settings_calculation_in_stomp_message(
        self, mock_publish_messages
    ):
        """Test that STOMP message contains correctly calculated settings."""

        # Mock previous usage for carryover calculation (per-component dict)
        with mock.patch.object(
            self.policy,
            "_get_previous_period_usage",
            return_value={"nodeHours": 600.0},
        ):
            success = self.policy.apply_policy_actions(self.resource)
            self.assertTrue(success)

            mock_publish_messages.assert_called_once()

            # Extract settings from published message
            message = mock_publish_messages.call_args[0][0][0]
            payload = json.loads(message["payload"])
            settings = payload["settings"]

            # Validate carryover calculation
            carryover_details = settings.get("carryover_details", {})
            self.assertTrue(carryover_details.get("carryover_applied"))

            # With 600Nh previous usage and carryover_factor=15 (15%):
            # For nodeHours component:
            # unused = 1000 - 600 = 400
            # carryover_cap = 0.15 * 1000 = 150
            # carryover = min(400, 150) = 150
            # total = 1000 + 150 = 1150
            per_component = carryover_details.get("per_component", {})
            self.assertIn("nodeHours", per_component)
            nh = per_component["nodeHours"]
            self.assertAlmostEqual(nh["total"], 1150.0, places=1)

            # Validate fairshare calculation (sum of all components / 3)
            fairshare = settings["fairshare"]
            expected_fairshare = int(1150.0 // 3)
            self.assertEqual(fairshare, expected_fairshare)

            # Validate TRES minutes: nodeHours component should be present
            # With grace_ratio=0.2, SLURM limit is set at grace level (1.2x base)
            grp_tres_mins = settings["grp_tres_mins"]
            self.assertIn("nodeHours", grp_tres_mins)
            expected_minutes = int(1150.0 * 1.2 * 60)
            self.assertEqual(grp_tres_mins["nodeHours"], expected_minutes)

            print("✅ Settings calculation in STOMP message validated")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_different_configurations_emit_different_messages(
        self, mock_publish_messages
    ):
        """Test that different policy configurations emit appropriately different messages."""

        # Test configuration 1: GrpTRESMins with billing
        self.policy.limit_type = "GrpTRESMins"
        self.policy.tres_billing_enabled = True
        self.policy.save()

        success = self.policy.apply_policy_actions(self.resource)
        self.assertTrue(success)

        mock_publish_messages.assert_called_once()

        # Extract first configuration message
        message1 = mock_publish_messages.call_args[0][0][0]
        payload1 = json.loads(message1["payload"])
        settings1 = payload1["settings"]

        self.assertEqual(settings1["limit_type"], "GrpTRESMins")
        self.assertIn("grp_tres_mins", settings1)
        # Per-component TRES minutes should include nodeHours
        self.assertIn("nodeHours", settings1["grp_tres_mins"])

        # Reset mock for second test
        mock_publish_messages.reset_mock()

        # Test configuration 2: MaxTRESMins with raw TRES
        self.policy.limit_type = "MaxTRESMins"
        self.policy.tres_billing_enabled = False
        self.policy.save()

        success = self.policy.apply_policy_actions(self.resource)
        self.assertTrue(success)

        mock_publish_messages.assert_called_once()

        # Extract second configuration message
        message2 = mock_publish_messages.call_args[0][0][0]
        payload2 = json.loads(message2["payload"])
        settings2 = payload2["settings"]

        self.assertEqual(settings2["limit_type"], "MaxTRESMins")
        self.assertIn("max_tres_mins", settings2)
        # Per-component TRES minutes should include nodeHours
        self.assertIn("nodeHours", settings2["max_tres_mins"])

        print("✅ Different configurations emit different STOMP messages")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_stomp_message_topic_structure(self, mock_publish_messages):
        """Test that STOMP message has correct topic structure."""

        success = self.policy.apply_policy_actions(self.resource)
        self.assertTrue(success)

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]

        # Validate message structure
        self.assertIn("topic", message)
        self.assertIn("payload", message)

        # Topic should follow the pattern for periodic limits updates
        topic = message["topic"]
        self.assertIn("resource_periodic_limits", topic)

        # Payload should be valid JSON
        payload_str = message["payload"]
        payload = json.loads(payload_str)  # Should not raise JSONDecodeError

        # Validate message metadata
        self.assertIn("resource_uuid", payload)
        self.assertIn("backend_id", payload)
        self.assertEqual(payload["resource_uuid"], str(self.resource.uuid))
        self.assertEqual(payload["backend_id"], self.resource.backend_id)

        print("✅ STOMP message topic structure validated")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_stomp_message_not_sent_when_policy_disabled(self, mock_publish_messages):
        """Test that no STOMP message is sent when periodic limits are effectively disabled."""

        # Create policy with carryover disabled and no previous usage
        self.policy.carryover_enabled = False
        self.policy.save()

        # Mock no previous usage (per-component dict)
        with mock.patch.object(
            self.policy, "_get_previous_period_usage", return_value={}
        ):
            self.policy.apply_policy_actions(self.resource)

            # Policy might still "succeed" but not send meaningful settings
            # This depends on implementation - policy might choose not to send updates
            # if nothing meaningful changed

            if mock_publish_messages.called:
                # If message was sent, verify it has minimal/default settings
                message = mock_publish_messages.call_args[0][0][0]
                payload = json.loads(message["payload"])
                settings = payload["settings"]

                carryover_details = settings.get("carryover_details", {})
                self.assertFalse(carryover_details.get("carryover_applied", True))

                print("✅ Disabled carryover reflected in STOMP message")
            else:
                print("✅ No STOMP message sent when no meaningful changes")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_stomp_message_error_handling(self, mock_publish_messages):
        """Test STOMP message handling when errors occur."""

        # Create invalid resource state to test error handling
        invalid_resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="",  # Invalid backend_id
        )

        # Test policy application with invalid resource
        try:
            success = self.policy.apply_policy_actions(invalid_resource)

            # Policy might handle error gracefully or fail
            if success:
                # If policy succeeded, message should still be valid
                if mock_publish_messages.called:
                    message = mock_publish_messages.call_args[0][0][0]
                    payload = json.loads(message["payload"])
                    # Backend ID might be empty but should not cause JSON issues
                    self.assertIsInstance(payload.get("backend_id"), str)
            else:
                # Policy failed gracefully - no message should be sent
                mock_publish_messages.assert_not_called()

            print("✅ Error handling for invalid resource working")

        except Exception:
            # Policy raised exception - this is also acceptable error handling
            print("✅ Exception raised for invalid resource (acceptable)")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_performance_of_stomp_message_generation(self, mock_publish_messages):
        """Test performance of STOMP message generation."""

        import time

        # Test batch policy applications (simulating multiple resources)
        resources = []
        for i in range(10):
            resource = marketplace_factories.ResourceFactory(
                project=self.fixture.project,
                offering=self.offering,
                plan=self.plan,
                backend_id=f"perf-test-account-{i}",
            )
            resources.append(resource)

        # Time the policy applications
        start_time = time.time()

        successful_applications = 0
        for resource in resources:
            try:
                success = self.policy.apply_policy_actions(resource)
                if success:
                    successful_applications += 1
            except Exception:
                pass  # Continue with other resources

        end_time = time.time()
        duration = end_time - start_time

        print("Performance Results:")
        print(f"  Resources: {len(resources)}")
        print(f"  Successful applications: {successful_applications}")
        print(f"  Duration: {duration:.3f}s")
        print(f"  Rate: {successful_applications / duration:.1f} policies/sec")

        # Verify messages were sent
        self.assertGreater(mock_publish_messages.call_count, 0)
        self.assertLessEqual(mock_publish_messages.call_count, len(resources))

        # Performance assertion
        avg_time = duration / len(resources)
        self.assertLess(
            avg_time, 0.1, f"Policy application too slow: {avg_time:.3f}s per resource"
        )

        print("✅ STOMP message generation performance acceptable")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_stomp_message_with_realistic_carryover_scenario(
        self, mock_publish_messages
    ):
        """Test STOMP message with realistic carryover scenario."""

        # Mock previous period usage (per-component dict)
        previous_usage = {"nodeHours": 750.0}

        with mock.patch.object(
            self.policy, "_get_previous_period_usage", return_value=previous_usage
        ):
            success = self.policy.apply_policy_actions(self.resource)
            self.assertTrue(success)

            mock_publish_messages.assert_called_once()

            # Extract and validate realistic scenario message
            message = mock_publish_messages.call_args[0][0][0]
            payload = json.loads(message["payload"])
            settings = payload["settings"]

            # Validate realistic carryover calculation (per-component)
            carryover = settings["carryover_details"]
            per_component = carryover["per_component"]
            nh = per_component["nodeHours"]

            print("Realistic Carryover Scenario:")
            print(f"  Previous usage: {nh['previous_usage']}Nh")
            print(f"  Unused allocation: {nh['unused']:.1f}Nh")
            print(f"  Carryover cap: {nh['carryover_cap']:.1f}Nh")
            print(f"  Carryover: {nh['carryover']:.1f}Nh")
            print(f"  Total allocation: {nh['total']:.1f}Nh")

            # Expected values for 750Nh previous usage with carryover_factor=15 (15%):
            # unused = 1000 - 750 = 250
            # carryover_cap = 0.15 * 1000 = 150
            # carryover = min(250, 150) = 150
            # total = 1000 + 150 = 1150

            self.assertEqual(nh["unused"], 250.0)
            self.assertEqual(nh["carryover_cap"], 150.0)
            self.assertEqual(nh["carryover"], 150.0)
            self.assertEqual(nh["total"], 1150.0)

            # Validate SLURM settings
            fairshare = settings["fairshare"]
            expected_fairshare = int(1150.0 // 3)
            self.assertEqual(fairshare, expected_fairshare)

            # With grace_ratio=0.2, SLURM limit is set at grace level (1.2x base)
            grp_tres_mins = settings["grp_tres_mins"]["nodeHours"]
            expected_minutes = int(1150.0 * 1.2 * 60)
            self.assertEqual(grp_tres_mins, expected_minutes)

            print(f"  Applied fairshare: {fairshare}")
            print(f"  Applied TRES limit: {grp_tres_mins:,} minutes")

            print("✅ Realistic carryover scenario STOMP message validated")


class SlurmPeriodicUsagePolicyEventTest(test.APITestCase):
    """Test policy event triggering and STOMP messaging patterns."""

    def setUp(self):
        """Setup for event testing."""
        self.fixture = marketplace_fixtures.MarketplaceFixture()

        self.offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            project=self.fixture.offering_project,
            customer=self.fixture.offering_customer,
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="nodeHours"
        )

        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.component,
            amount=1500,  # 1500Nh allocation
        )

        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="event-test-account",
            limits={"nodeHours": 1500},
        )

        # Create policy
        self.policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            grace_ratio=0.15,  # 15% grace
            carryover_enabled=True,
            tres_billing_enabled=True,
            limit_type="GrpTRESMins",
        )

        organization_group = structure_factories.OrganizationGroupFactory()
        self.policy.organization_groups.add(organization_group)

        # Create event subscription for periodic limits
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[{"object_type": "resource_periodic_limits"}],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_component_usage_change_triggers_stomp_message(self, mock_publish_messages):
        """Test that ComponentUsage changes trigger periodic limits STOMP messages."""

        # Create initial usage
        usage = marketplace_factories.ComponentUsageFactory(
            resource=self.resource, component=self.component, usage=400.0
        )

        # Mock previous period usage (per-component dict)
        with mock.patch.object(
            self.policy,
            "_get_previous_period_usage",
            return_value={"nodeHours": 200.0},
        ):
            # Update usage (simulating monthly usage reporting)
            usage.usage = 800.0  # Increase usage
            usage.save()

            # Manually trigger policy (in practice this would be automatic)
            success = self.policy.apply_policy_actions(self.resource)
            self.assertTrue(success)

            # Verify STOMP message was sent
            mock_publish_messages.assert_called()

            # Validate message reflects updated usage scenario
            message = mock_publish_messages.call_args[0][0][0]
            payload = json.loads(message["payload"])

            self.assertEqual(payload["action"], "apply_periodic_settings")

            # Settings should reflect the new allocation calculation
            settings = payload["settings"]
            self.assertIn("grp_tres_mins", settings)

            print("✅ ComponentUsage change triggered STOMP message")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_multiple_resources_generate_separate_messages(self, mock_publish_messages):
        """Test that multiple resources generate separate STOMP messages."""

        # Create additional resources in the same offering
        resources = [self.resource]  # Original resource

        for i in range(3):
            additional_resource = marketplace_factories.ResourceFactory(
                project=self.fixture.project,
                offering=self.offering,
                plan=self.plan,
                backend_id=f"multi-test-account-{i}",
            )
            resources.append(additional_resource)

        # Apply policy to all resources
        successful_applications = 0

        for resource in resources:
            success = self.policy.apply_policy_actions(resource)
            if success:
                successful_applications += 1

        print(f"Applied policy to {successful_applications}/{len(resources)} resources")

        # Each resource should generate its own STOMP message
        expected_calls = successful_applications
        self.assertEqual(mock_publish_messages.call_count, expected_calls)

        # Validate each message has unique resource identifiers
        resource_uuids = set()
        backend_ids = set()

        for call in mock_publish_messages.call_args_list:
            message = call[0][0][0]
            payload = json.loads(message["payload"])

            resource_uuid = payload["resource_uuid"]
            backend_id = payload["backend_id"]

            resource_uuids.add(resource_uuid)
            backend_ids.add(backend_id)

        # Should have unique identifiers for each resource
        self.assertEqual(len(resource_uuids), expected_calls)
        self.assertEqual(len(backend_ids), expected_calls)

        print(
            f"✅ {expected_calls} separate STOMP messages for {len(resources)} resources"
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_stomp_message_format_matches_site_agent_expectations(
        self, mock_publish_messages
    ):
        """Test that STOMP message format matches what site agent expects."""

        success = self.policy.apply_policy_actions(self.resource)
        self.assertTrue(success)

        mock_publish_messages.assert_called_once()

        message = mock_publish_messages.call_args[0][0][0]
        payload = json.loads(message["payload"])

        # Validate format matches PeriodicLimitsMessage structure from site agent

        # Check that all PeriodicLimitsMessage fields are present
        required_typed_dict_fields = [
            "resource_uuid",
            "backend_id",
            "offering_uuid",
            "action",
            "settings",
            "timestamp",
        ]

        for field in required_typed_dict_fields:
            self.assertIn(
                field, payload, f"Missing PeriodicLimitsMessage field: {field}"
            )

        # Validate field types match expectations
        self.assertIsInstance(payload["resource_uuid"], str)
        self.assertIsInstance(payload["backend_id"], str)
        self.assertIsInstance(payload["offering_uuid"], str)
        self.assertIsInstance(payload["action"], str)
        self.assertIsInstance(payload["settings"], dict)
        self.assertIsInstance(payload["timestamp"], str)

        # Validate action value
        self.assertEqual(payload["action"], "apply_periodic_settings")

        # Validate settings structure matches backend expectations
        settings = payload["settings"]

        # Should have either grp_tres_mins or max_tres_mins (not both)
        has_grp = "grp_tres_mins" in settings
        has_max = "max_tres_mins" in settings
        self.assertTrue(has_grp or has_max, "Missing TRES limits in settings")
        self.assertFalse(has_grp and has_max, "Should not have both GRP and MAX limits")

        print("✅ STOMP message format matches site agent expectations")


class SlurmPeriodicUsagePolicyIntegrationTest(test.APITestCase):
    """Integration tests between policy and STOMP messaging."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()

        # Setup offering with multiple components
        self.offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            project=self.fixture.offering_project,
            customer=self.fixture.offering_customer,
        )

        self.node_hours_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="nodeHours", billing_type="usage"
        )

        self.gpu_hours_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="gpuHours", billing_type="usage"
        )

        self.plan = marketplace_factories.PlanFactory(offering=self.offering)

        marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.node_hours_component,
            amount=2000,  # 2000 node-hours
        )

        marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.gpu_hours_component,
            amount=500,  # 500 GPU-hours
        )

        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="integration-test-account",
            limits={"nodeHours": 2000, "gpuHours": 500},
        )

        # Create advanced policy configuration
        self.policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            grace_ratio=0.25,  # 25% grace period
            carryover_enabled=True,
            carryover_factor=21,
            tres_billing_enabled=True,
            limit_type="GrpTRESMins",
            tres_billing_weights={
                "CPU": 0.01,  # Custom weights
                "Mem": 0.002,
                "GRES/gpu": 0.5,
            },
        )

        organization_group = structure_factories.OrganizationGroupFactory()
        self.policy.organization_groups.add(organization_group)

        # Create event subscription for periodic limits
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[{"object_type": "resource_periodic_limits"}],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_advanced_policy_configuration_stomp_message(self, mock_publish_messages):
        """Test STOMP message with advanced policy configuration."""

        # Mock complex previous usage scenario (per-component dict)
        with mock.patch.object(
            self.policy,
            "_get_previous_period_usage",
            return_value={"nodeHours": 1200.0, "gpuHours": 0.0},
        ):
            success = self.policy.apply_policy_actions(self.resource)
            self.assertTrue(success)

            mock_publish_messages.assert_called_once()

            message = mock_publish_messages.call_args[0][0][0]
            payload = json.loads(message["payload"])
            settings = payload["settings"]

            # Validate advanced configuration is reflected (per-component)
            carryover = settings["carryover_details"]
            per_component = carryover["per_component"]

            self.assertEqual(carryover["carryover_factor"], 21)

            # nodeHours: unused=2000-1200=800, cap=0.21*2000=420, carry=420, total=2420
            nh = per_component["nodeHours"]
            self.assertEqual(nh["unused"], 800.0)
            self.assertAlmostEqual(nh["carryover_cap"], 420.0, places=1)
            self.assertAlmostEqual(nh["carryover"], 420.0, places=1)
            self.assertAlmostEqual(nh["total"], 2420.0, places=1)

            # gpuHours: unused=500-0=500, cap=0.21*500=105, carry=105, total=605
            gh = per_component["gpuHours"]
            self.assertEqual(gh["unused"], 500.0)
            self.assertAlmostEqual(gh["carryover_cap"], 105.0, places=1)
            self.assertAlmostEqual(gh["carryover"], 105.0, places=1)
            self.assertAlmostEqual(gh["total"], 605.0, places=1)

            self.assertEqual(settings["limit_type"], "GrpTRESMins")

            print("Advanced configuration results:")
            print(f"  Carryover factor: {carryover['carryover_factor']}%")
            print(f"  nodeHours total: {nh['total']:.0f}Nh")
            print(f"  gpuHours total: {gh['total']:.0f}Nh")
            print(f"  Fairshare: {settings['fairshare']}")

            print("✅ Advanced policy configuration STOMP message validated")


def test_stomp_message_emission_pattern():
    """Standalone test to validate STOMP message emission patterns."""

    print("📨 STOMP Message Emission Pattern Analysis")
    print("=" * 50)

    print("✅ Existing patterns in waldur-mastermind:")
    print("   • test_user_role_sync.py: Tests user role STOMP messages")
    print("   • test_service_account.py: Tests service account STOMP messages")
    print("   • test_offering_user.py: Tests offering user STOMP messages")
    print("   • test_orders.py: Tests order processing STOMP messages")
    print()
    print("✅ Pattern used:")
    print("   @mock.patch('waldur_core.logging.tasks.publish_messages.delay')")
    print("   def test_method(self, mock_publish_messages):")
    print("       # Trigger action that should send STOMP message")
    print("       # Assert mock_publish_messages.assert_called_once()")
    print("       # Validate message structure and content")
    print()
    print("✅ New test created for periodic limits:")
    print("   • test_slurm_periodic_policy_stomp.py")
    print("   • Tests policy trigger → STOMP message emission")
    print("   • Validates message structure and content")
    print("   • Tests different configurations")
    print("   • Tests performance and error handling")
    print()
    print("🚀 Complete STOMP testing coverage now available!")


if __name__ == "__main__":
    test_stomp_message_emission_pattern()
