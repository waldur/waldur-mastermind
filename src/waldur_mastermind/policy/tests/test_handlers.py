from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.policy import models
from waldur_mastermind.policy.tests import factories


class TestCostPolicyDeletionHandler(test.APITransactionTestCase):
    def setUp(self):
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url()
        self.client = test.APIClient()

        self.user = structure_factories.UserFactory(is_staff=True)
        self.project = structure_factories.ProjectFactory()
        self.client.force_authenticate(user=self.user)

        self.policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
        )

    def test_policy_deletion_triggers_reset_actions(self):
        policy_url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

        response = self.client.delete(policy_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ProjectEstimatedCostPolicy.objects.filter(id=self.policy.id).exists()
        )

    def test_policy_deletion_with_multiple_policies(self):
        policy2 = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
        )

        policy_url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        response = self.client.delete(policy_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ProjectEstimatedCostPolicy.objects.filter(id=self.policy.id).exists()
        )
        self.assertTrue(
            models.ProjectEstimatedCostPolicy.objects.filter(id=policy2.id).exists()
        )

    def test_policy_deletion_with_invalid_permissions(self):
        another_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=another_user)

        policy_url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        response = self.client.delete(policy_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            models.ProjectEstimatedCostPolicy.objects.filter(id=self.policy.id).exists()
        )
