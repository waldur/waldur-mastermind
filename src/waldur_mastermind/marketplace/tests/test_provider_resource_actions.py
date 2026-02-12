from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class ResourceSetOkTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=models.Resource.States.ERRED,
        )
        # Offering owner can performing this action
        self.owner = self.fixture.owner
        self.offering.customer.add_user(self.owner, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_STATE)

    def set_state_ok(self, user):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "set_state_ok"
        )
        return self.client.post(url)

    def test_service_provider_can_set_resource_state_to_ok(self):
        response = self.set_state_ok(self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, models.Resource.States.OK)

    def test_regular_user_cannot_set_resource_state_to_ok(self):
        response = self.set_state_ok(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_admin_cannot_set_resource_state_to_ok(self):
        response = self.set_state_ok(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_allowed_states(self):
        for state in [
            models.Resource.States.CREATING,
            models.Resource.States.UPDATING,
            models.Resource.States.TERMINATING,
        ]:
            self.resource.state = state
            self.resource.save()
            response = self.set_state_ok(self.owner)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.resource.refresh_from_db()
            self.assertEqual(self.resource.state, models.Resource.States.OK)
