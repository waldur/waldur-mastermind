from ddt import data, ddt
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class RuntimeStatesViewSetTestCase(APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.url = reverse("marketplace-runtime-states-list")

    @data("staff", "owner", "admin", "manager")
    def test_runtime_state_with_project_uuid(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"project_uuid": self.project.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("staff", "owner", "admin", "manager")
    def test_unscoped_request_returns_400(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_runtime_states_are_scoped_by_offering(self):
        offering_resource = self.fixture.resource
        offering_resource.backend_metadata = {"runtime_state": "ACTIVE"}
        offering_resource.save()

        other_resource = factories.ResourceFactory(project=self.project)
        other_resource.backend_metadata = {"runtime_state": "SHUTOFF"}
        other_resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": offering_resource.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([option["value"] for option in response.data], ["ACTIVE"])

    def test_runtime_states_are_scoped_by_customer(self):
        resource = self.fixture.resource
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.save()

        other_customer = structure_factories.CustomerFactory()
        other_project = structure_factories.ProjectFactory(customer=other_customer)
        other_resource = factories.ResourceFactory(project=other_project)
        other_resource.backend_metadata = {"runtime_state": "SHUTOFF"}
        other_resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([option["value"] for option in response.data], ["ACTIVE"])

    def test_runtime_states_are_scoped_by_category(self):
        resource = self.fixture.resource
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.save()

        other_resource = factories.ResourceFactory(project=self.project)
        other_resource.backend_metadata = {"runtime_state": "SHUTOFF"}
        other_resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {"category_uuid": resource.offering.category.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([option["value"] for option in response.data], ["ACTIVE"])

    def test_service_owner_sees_runtime_states_of_own_offering(self):
        # The service owner holds no role in the consumer project, so the states
        # are only reachable through the service provider scope.
        resource = self.fixture.resource
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.save()

        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(
            self.url, {"offering_uuid": resource.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([option["value"] for option in response.data], ["ACTIVE"])

    def test_offering_runtime_states_are_hidden_from_unrelated_user(self):
        resource = self.fixture.resource
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.save()

        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(
            self.url, {"offering_uuid": resource.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_terminated_resources_are_excluded(self):
        resource = self.fixture.resource
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.set_state_terminated()
        resource.save()

        active_resource = factories.ResourceFactory(
            project=self.project,
            offering=self.fixture.offering,
        )
        active_resource.backend_metadata = {"runtime_state": "SHUTOFF"}
        active_resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"project_uuid": self.project.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([option["value"] for option in response.data], ["SHUTOFF"])

    def test_browsable_api_does_not_crash(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {"project_uuid": self.project.uuid.hex},
            HTTP_ACCEPT="text/html",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_mismatched_project_and_customer_returns_empty(self):
        other_customer = structure_factories.CustomerFactory()
        other_project = structure_factories.ProjectFactory(customer=other_customer)
        resource = factories.ResourceFactory(project=other_project)
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "project_uuid": other_project.uuid.hex,
                "customer_uuid": self.fixture.customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_offering_and_customer_scope_limits_provider_resources(self):
        resource = self.fixture.resource
        resource.backend_metadata = {"runtime_state": "ACTIVE"}
        resource.save()

        other_customer = structure_factories.CustomerFactory()
        other_project = structure_factories.ProjectFactory(customer=other_customer)
        other_resource = factories.ResourceFactory(
            project=other_project,
            offering=self.fixture.offering,
        )
        other_resource.backend_metadata = {"runtime_state": "SHUTOFF"}
        other_resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "customer_uuid": self.fixture.customer.uuid.hex,
                "offering_uuid": self.fixture.offering.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([option["value"] for option in response.data], ["ACTIVE"])
