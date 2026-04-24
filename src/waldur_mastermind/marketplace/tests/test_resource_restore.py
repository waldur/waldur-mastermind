from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import enums, models
from waldur_mastermind.marketplace.tests import factories


class ResourceRestoreTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=models.Resource.States.TERMINATED,
        )

        # Allow restore by default
        self.offering.plugin_options["can_restore_resource"] = True
        self.offering.save()

    def test_restore_terminated_resource(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, models.Resource.States.CREATING)

        order = models.Order.objects.get(
            resource=self.resource, type=enums.OrderTypes.RESTORE
        )
        self.assertIn(
            order.state,
            [
                enums.OrderStates.PENDING_CONSUMER,
                enums.OrderStates.PENDING_PROVIDER,
                enums.OrderStates.EXECUTING,
            ],
        )

    def test_restore_active_resource_fails(self):
        self.resource.state = models.Resource.States.OK
        self.resource.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_restore_not_allowed_for_regular_user(self):
        self.client.force_authenticate(self.fixture.user)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_restore_allowed_for_service_provider_owner(self):
        sp_owner = UserFactory()
        self.offering.customer.add_user(sp_owner, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_STATE)

        self.client.force_authenticate(sp_owner)
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "restore"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_restore_not_allowed_for_consumer_organization_owner(self):
        # Owner of the consuming organization should NOT be able to restore
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_restore_disabled_by_plugin(self):
        self.offering.plugin_options["can_restore_resource"] = False
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Restoring resource is not supported for this offering type.",
            str(response.data),
        )
