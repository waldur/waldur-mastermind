from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import enums, models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories


class ResourceRestoreTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.TERMINATED,
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
        self.assertEqual(self.resource.state, ResourceStates.CREATING)

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
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Resource must be in TERMINATED state to be restored.", str(response.data)
        )

    def test_restore_permissions(self):
        # User who is not staff/owner/manager should not be able to restore
        self.client.force_authenticate(self.fixture.user)
        url = factories.ResourceFactory.get_url(self.resource, "restore")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Owner should be able to restore
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_restore_disabled_by_plugin(self):
        # Arrange
        self.offering.plugin_options["can_restore_resource"] = False
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "restore")

        # Act
        # Default behavior is False, so it should fail
        response = self.client.post(url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Restoring resource is not supported for this offering type.",
            str(response.data),
        )
