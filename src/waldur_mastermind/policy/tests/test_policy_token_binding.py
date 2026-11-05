"""Where an owner's personal access token bound to a project may configure
cost policies: on its project, whose policies it reaches through their scope,
and on no other project of the organization."""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.policy import models
from waldur_mastermind.policy.tests import factories


@override_config(PAT_ENABLED=True)
class ProjectPolicyTokenBindingTest(test.APITestCase):
    def setUp(self):
        self.owner = structure_factories.UserFactory(
            can_use_personal_access_tokens=True
        )
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.project_a = structure_factories.ProjectFactory(customer=self.customer)
        self.project_b = structure_factories.ProjectFactory(customer=self.customer)
        self.policy_a = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project_a, limit_cost=10
        )
        self.policy_b = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project_b, limit_cost=10
        )

        _, token = create_pat(
            self.owner,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            allowed_scopes=[
                {
                    "content_type_id": ContentType.objects.get_for_model(
                        self.project_a
                    ).id,
                    "object_id": self.project_a.id,
                }
            ],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _patch(self, policy):
        return self.client.patch(
            factories.ProjectEstimatedCostPolicyFactory.get_url(policy),
            {"limit_cost": 20},
        )

    def _create(self, project):
        return self.client.post(
            factories.ProjectEstimatedCostPolicyFactory.get_list_url(),
            {
                "limit_cost": 100,
                "actions": "notify_organization_owners",
                "scope": structure_factories.ProjectFactory.get_url(project),
            },
        )

    def test_updates_its_projects_policy(self):
        response = self._patch(self.policy_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.policy_a.refresh_from_db()
        self.assertEqual(self.policy_a.limit_cost, 20)

    def test_cannot_update_another_projects_policy(self):
        response = self._patch(self.policy_b)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.policy_b.refresh_from_db()
        self.assertEqual(self.policy_b.limit_cost, 10)

    def test_creates_a_policy_for_its_project(self):
        response = self._create(self.project_a)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_cannot_create_a_policy_for_another_project(self):
        response = self._create(self.project_b)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            models.ProjectEstimatedCostPolicy.objects.filter(
                scope=self.project_b
            ).count(),
            1,
        )
