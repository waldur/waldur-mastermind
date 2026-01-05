from unittest import mock

import pytest
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy import models
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
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


class TestIsMockedSkipsPolicyCheck(test.APITransactionTestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)

    def test_is_mocked_skips_policy_check(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_scope_from_observable_object",
            return_value=None,
        ) as actions_mock:
            resource = marketplace_models.Resource(
                project=self.project, offering=marketplace_factories.OfferingFactory()
            )
            resource.is_mocked = True
            resource.save()
            actions_mock.assert_not_called()

            new_resource = marketplace_models.Resource(
                project=self.project, offering=marketplace_factories.OfferingFactory()
            )
            new_resource.save()
            actions_mock.assert_called_once()


class TestPolicySignalHandlerIsRegistered(test.APITransactionTestCase):
    """
    Test that policy signal handlers are properly registered and fire.

    This validates that the signal handler closure is not garbage collected.
    The fix requires weak=False in apps.py signal registration.
    """

    def test_customer_policy_blocks_resource_creation_via_signal(self):
        """
        When a CustomerEstimatedCostPolicy has fired with block_creation action,
        creating a new Resource should raise PolicyException via the signal handler.
        """
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        offering = marketplace_factories.OfferingFactory()

        # Create policy with has_fired=True and blocking action
        factories.CustomerEstimatedCostPolicyFactory(
            scope=customer,
            actions="block_creation_of_new_resources",
            has_fired=True,
        )

        # Saving a new Resource should trigger the signal handler
        # which should raise PolicyException
        with pytest.raises(PolicyException):
            resource = marketplace_models.Resource(
                project=project,
                offering=offering,
                name="test-resource",
            )
            resource.save()

    def test_project_policy_blocks_resource_creation_via_signal(self):
        """
        When a ProjectEstimatedCostPolicy has fired with block_creation action,
        creating a new Resource should raise PolicyException via the signal handler.
        """
        project = structure_factories.ProjectFactory()
        offering = marketplace_factories.OfferingFactory()

        # Create policy with has_fired=True and blocking action
        factories.ProjectEstimatedCostPolicyFactory(
            scope=project,
            actions="block_creation_of_new_resources",
            has_fired=True,
        )

        # Saving a new Resource should trigger the signal handler
        # which should raise PolicyException
        with pytest.raises(PolicyException):
            resource = marketplace_models.Resource(
                project=project,
                offering=offering,
                name="test-resource",
            )
            resource.save()
