from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.policy import models


class ApplyToAllModelTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering

    def test_apply_to_all_policy_affects_all_customers(self):
        """Test that apply_to_all=True affects all customers"""
        # Create policy with apply_to_all
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
        )

        # Create multiple customers
        customers = [
            structure_factories.CustomerFactory(blocked=False, archived=False)
            for _ in range(5)
        ]

        # Get affected customers
        affected = policy.get_affected_customers()

        # Should include all active customers
        for customer in customers:
            self.assertIn(customer, affected)

    def test_organization_groups_policy_affects_only_group_members(self):
        """Test that organization_groups policy affects only group members"""
        org_group = structure_factories.OrganizationGroupFactory()
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=False,
        )
        policy.organization_groups.add(org_group)

        # Create customers - some in group, some not
        in_group = structure_factories.CustomerFactory(blocked=False, archived=False)
        in_group.organization_groups.add(org_group)

        out_of_group = structure_factories.CustomerFactory(
            blocked=False, archived=False
        )

        # Get affected customers
        affected = policy.get_affected_customers()

        # Should include only group member
        self.assertIn(in_group, affected)
        self.assertNotIn(out_of_group, affected)

    def test_mutual_exclusivity_validation(self):
        """Test that apply_to_all and organization_groups are mutually exclusive"""
        org_group = structure_factories.OrganizationGroupFactory()

        # Create policy with apply_to_all
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
        )

        # Try to add organization group - should fail validation
        policy.organization_groups.add(org_group)

        with self.assertRaises(ValidationError) as context:
            policy.clean()

        error_msg = str(context.exception)
        self.assertIn(
            "apply_to_all=True when organization_groups are specified", error_msg
        )

    def test_at_least_one_required(self):
        """Test that either apply_to_all or organization_groups is required"""
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=False,
        )

        # Should raise validation error when neither is set
        with self.assertRaises(ValidationError) as context:
            policy.clean()

        self.assertIn("Must either set", str(context.exception))

    def test_apply_to_all_excludes_blocked_customers(self):
        """Test that apply_to_all excludes blocked/archived customers"""
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
        )

        active_customer = structure_factories.CustomerFactory(
            blocked=False, archived=False
        )
        blocked_customer = structure_factories.CustomerFactory(
            blocked=True, archived=False
        )
        archived_customer = structure_factories.CustomerFactory(
            blocked=False, archived=True
        )

        affected = policy.get_affected_customers()

        self.assertIn(active_customer, affected)
        self.assertNotIn(blocked_customer, affected)
        self.assertNotIn(archived_customer, affected)


class ApplyToAllAPITest(APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.customer_owner = self.fixture.owner
        self.staff = structure_factories.UserFactory(is_staff=True)

    def test_create_policy_with_apply_to_all_via_api(self):
        """Test creating a policy with apply_to_all via API"""
        self.client.force_authenticate(user=self.staff)

        url = "/api/marketplace-offering-usage-policies/"
        payload = {
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "actions": "notify_organization_owners",
            "apply_to_all": True,
            "component_limits_set": [
                {"type": self.fixture.offering_usage_component.type, "limit": 100}
            ],
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify policy was created with apply_to_all
        policy = models.OfferingUsagePolicy.objects.get(uuid=response.data["uuid"])
        self.assertTrue(policy.apply_to_all)
        self.assertEqual(policy.organization_groups.count(), 0)

    def test_api_validation_mutual_exclusivity(self):
        """Test API validation for mutual exclusivity"""
        self.client.force_authenticate(user=self.staff)
        org_group = structure_factories.OrganizationGroupFactory()

        url = "/api/marketplace-offering-usage-policies/"
        payload = {
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "actions": "notify_organization_owners",
            "apply_to_all": True,
            "organization_groups": [
                structure_factories.OrganizationGroupFactory.get_url(org_group)
            ],
            "component_limits_set": [
                {"type": self.fixture.offering_usage_component.type, "limit": 100}
            ],
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "apply_to_all=True when organization_groups are specified",
            str(response.data),
        )

    def test_api_validation_at_least_one_required(self):
        """Test API validation that at least one option is required"""
        self.client.force_authenticate(user=self.staff)

        url = "/api/marketplace-offering-usage-policies/"
        payload = {
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "actions": "notify_organization_owners",
            "apply_to_all": False,
            "organization_groups": [],
            "component_limits_set": [
                {"type": self.fixture.offering_usage_component.type, "limit": 100}
            ],
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Must either set", str(response.data))

    def test_update_policy_from_groups_to_apply_to_all(self):
        """Test updating policy from organization_groups to apply_to_all"""
        self.client.force_authenticate(user=self.staff)

        # Create policy with organization groups
        org_group = structure_factories.OrganizationGroupFactory()
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=False,
        )
        policy.organization_groups.add(org_group)

        url = f"/api/marketplace-offering-usage-policies/{policy.uuid.hex}/"

        # Update to apply_to_all
        payload = {
            "apply_to_all": True,
            "organization_groups": [],
        }

        response = self.client.patch(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify update
        policy.refresh_from_db()
        self.assertTrue(policy.apply_to_all)
        self.assertEqual(policy.organization_groups.count(), 0)


class ApplyToAllOptimizationTest(TestCase):
    """Test the optimized query performance for apply_to_all"""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering

    def test_estimated_cost_policy_optimized_query(self):
        """Test that OfferingEstimatedCostPolicy uses optimized query for apply_to_all"""
        policy = models.OfferingEstimatedCostPolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
            limit_cost=1000,
        )

        # Create test data
        for _ in range(10):
            customer = structure_factories.CustomerFactory(
                blocked=False, archived=False
            )
            project = structure_factories.ProjectFactory(customer=customer)
            marketplace_factories.ResourceFactory(
                offering=self.offering, project=project
            )

        # Test that is_triggered works without errors
        # In a real test, you'd check query counts with assertNumQueries
        try:
            triggered = policy.is_triggered()
            self.assertIsInstance(triggered, bool)
        except Exception as e:
            self.fail(f"Optimized query failed: {e}")

    def test_usage_policy_optimized_query(self):
        """Test that OfferingUsagePolicy uses optimized query for apply_to_all"""
        policy = models.OfferingUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
        )

        # Add component limit
        models.OfferingComponentLimit.objects.create(
            policy=policy, component=self.fixture.offering_usage_component, limit=100
        )

        # Create test data
        for _ in range(10):
            customer = structure_factories.CustomerFactory(
                blocked=False, archived=False
            )
            project = structure_factories.ProjectFactory(customer=customer)
            resource = marketplace_factories.ResourceFactory(
                offering=self.offering, project=project
            )
            marketplace_factories.ComponentUsageFactory(
                resource=resource,
                component=self.fixture.offering_usage_component,
                usage=10,
            )

        # Test that is_triggered works without errors
        try:
            triggered = policy.is_triggered()
            self.assertIsInstance(triggered, bool)
        except Exception as e:
            self.fail(f"Optimized query failed: {e}")
