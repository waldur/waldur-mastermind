from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging.tests import factories as logging_factories
from waldur_core.logging.utils import ObservableObjectType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class BackendResourcePermissionsTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.BackendResourceFactory.get_list_url()
        CustomerRole.OWNER.add_permission(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES
        )
        OfferingRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES
        )

    @data("staff", "offering_owner", "offering_manager", "service_manager")
    def test_user_can_create_backend_resources(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        test_backend_id = "test-backend-id-00"
        payload = {
            "name": "test-resource",
            "project": self.fixture.project.uuid.hex,
            "offering": self.fixture.offering.uuid.hex,
            "backend_id": test_backend_id,
            "backend_metadata": {"test-attr": "test-val"},
        }
        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.BackendResource.objects.count(), 1)
        backend_resource = models.BackendResource.objects.first()
        self.assertEqual(backend_resource.backend_id, test_backend_id)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_create_backend_resources(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        payload = {
            "name": "test-resource",
            "project": self.fixture.project.uuid.hex,
            "offering": self.fixture.offering.uuid.hex,
            "backend_id": "test-backend-id-00",
            "backend_metadata": {"test-attr": "test-val"},
        }
        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "offering_owner", "offering_manager", "service_manager")
    def test_user_can_see_backend_resources(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        backend_resource = factories.BackendResourceFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
        )

        url = factories.BackendResourceFactory.get_url(backend_resource)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["backend_id"], backend_resource.backend_id)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_see_backend_resources(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        backend_resource = factories.BackendResourceFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
        )

        url = factories.BackendResourceFactory.get_url(backend_resource)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BackendResourceImportTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.project = self.fixture.project
        self.backend_resource = factories.BackendResourceFactory(
            offering=self.offering, project=self.project
        )

    def test_backend_resources_import(self):
        self.client.force_login(self.fixture.staff)
        url = factories.BackendResourceFactory.get_url(
            self.backend_resource, "import_resource"
        )
        payload = {"plan": self.fixture.plan.uuid.hex}
        response = self.client.post(url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Resource.objects.filter(
                backend_id=self.backend_resource.backend_id
            ).exists()
        )
        resource = models.Resource.objects.get(
            backend_id=self.backend_resource.backend_id
        )
        self.assertEqual(resource.offering, self.offering)
        self.assertEqual(resource.project, self.project)
        self.assertEqual(resource.backend_id, self.backend_resource.backend_id)
        self.assertEqual(resource.name, self.backend_resource.name)
        self.assertEqual(resource.plan, self.fixture.plan)
        self.assertFalse(
            models.BackendResource.objects.filter(
                backend_id=self.backend_resource.backend_id
            ).exists()
        )
        self.assertTrue(
            models.Order.objects.filter(
                resource=resource,
                created_by=self.fixture.staff,
            ).exists()
        )


@ddt
class BackendResourceRequestTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering

        self.resource_request = factories.BackendResourceRequestFactory(
            offering=self.offering
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES
        )
        OfferingRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES
        )

    @data("staff", "offering_owner", "offering_manager", "service_manager")
    def test_user_can_process_backend_resource_request(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        url = factories.BackendResourceRequestFactory.get_url(
            self.resource_request, "start_processing"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource_request.refresh_from_db()
        self.assertEqual(
            self.resource_request.state, models.BackendResourceRequest.States.PROCESSING
        )

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_process_backend_resource_request(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        url = factories.BackendResourceRequestFactory.get_url(
            self.resource_request, "start_processing"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "offering_owner", "offering_manager", "service_manager")
    def test_user_can_set_done_backend_resource_request(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        self.resource_request.start_processing()
        self.resource_request.save()

        url = factories.BackendResourceRequestFactory.get_url(
            self.resource_request, "set_done"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource_request.refresh_from_db()
        self.assertEqual(
            self.resource_request.state, models.BackendResourceRequest.States.DONE
        )

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_set_done_backend_resource_request(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        url = factories.BackendResourceRequestFactory.get_url(
            self.resource_request, "set_done"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "offering_owner", "offering_manager", "service_manager")
    def test_user_can_set_erred_backend_resource_request(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        url = factories.BackendResourceRequestFactory.get_url(
            self.resource_request, "set_erred"
        )

        payload = {"error_message": "test error", "error_traceback": "test traceback"}
        response = self.client.post(url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource_request.refresh_from_db()
        self.assertEqual(
            self.resource_request.state, models.BackendResourceRequest.States.ERRED
        )
        self.assertEqual(self.resource_request.error_message, payload["error_message"])
        self.assertEqual(
            self.resource_request.error_traceback, payload["error_traceback"]
        )

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_set_erred_backend_resource_request(self, role):
        user = getattr(self.fixture, role)
        self.client.force_login(user)

        url = factories.BackendResourceRequestFactory.get_url(
            self.resource_request, "set_done"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.logging.tasks.publish_messages")
    def test_create_backend_resource_request(self, mock_publish_messages):
        logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[
                {"object_type": ObservableObjectType.IMPORTABLE_RESOURCES.value}
            ],
        )

        self.client.force_login(self.fixture.staff)
        url = factories.BackendResourceRequestFactory.get_list_url()
        payload = {
            "offering": self.offering.uuid.hex,
        }
        response = self.client.post(url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["state"], models.BackendResourceRequest.States.SENT
        )
        mock_publish_messages.delay.assert_called_once()
