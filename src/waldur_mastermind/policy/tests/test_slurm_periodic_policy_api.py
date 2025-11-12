from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory


@ddt
class SlurmPeriodicUsagePolicyGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()

        # Add user as owner to the customer
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url()

    @data("staff")
    def test_staff_can_get_policy(self, user_type):
        if user_type == "staff":
            user = structure_factories.UserFactory(is_staff=True)
        else:
            user = self.user

        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_offering_owner_can_get_policy(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unauthorized_user_cannot_get_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class SlurmPeriodicUsagePolicyCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        # Create organization group for the policy
        self.organization_group = structure_factories.OrganizationGroupFactory()

        # Create offering component for component limits
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="nodeHours"
        )

        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url()

    def _create_policy_payload(self):
        return {
            "actions": "notify_organization_owners",
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "organization_groups": [
                structure_factories.OrganizationGroupFactory.get_url(
                    self.organization_group
                )
            ],
            "component_limits_set": [
                {"type": self.offering_component.type, "limit": 1000}
            ],
            "limit_type": "GrpTRESMins",
            "tres_billing_enabled": True,
            "grace_ratio": 0.2,
            "carryover_enabled": True,
            "fairshare_decay_half_life": 15,
            "raw_usage_reset": True,
            "qos_strategy": "threshold",
        }

    def test_staff_can_create_policy(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        payload = self._create_policy_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        policy = SlurmPeriodicUsagePolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.limit_type, "GrpTRESMins")
        self.assertTrue(policy.tres_billing_enabled)
        self.assertEqual(policy.grace_ratio, 0.2)

    def test_offering_owner_can_create_policy(self):
        self.client.force_authenticate(self.user)

        payload = self._create_policy_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unauthorized_user_cannot_create_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        payload = self._create_policy_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        payload = self._create_policy_payload()
        payload["actions"] = "notify_organization_owners,non_existent_action"

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class SlurmPeriodicUsagePolicyUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(self.policy)

    def test_staff_can_update_policy(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        payload = {"grace_ratio": 0.3}
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.policy.refresh_from_db()
        self.assertEqual(self.policy.grace_ratio, 0.3)

    def test_offering_owner_can_update_policy(self):
        self.client.force_authenticate(self.user)

        payload = {"carryover_enabled": False}
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.policy.refresh_from_db()
        self.assertFalse(self.policy.carryover_enabled)

    def test_unauthorized_user_cannot_update_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        payload = {"grace_ratio": 0.5}
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class SlurmPeriodicUsagePolicyDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.policy = SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(self.policy)

    def test_staff_can_delete_policy(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_offering_owner_can_delete_policy(self):
        self.client.force_authenticate(self.user)

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthorized_user_cannot_delete_policy(self):
        unauthorized_user = structure_factories.UserFactory()
        self.client.force_authenticate(unauthorized_user)

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SlurmPeriodicUsagePolicyActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)

        self.url = SlurmPeriodicUsagePolicyFactory.get_list_url("actions")

    def test_get_available_actions(self):
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        self.assertIn("notify_organization_owners", response.data)
