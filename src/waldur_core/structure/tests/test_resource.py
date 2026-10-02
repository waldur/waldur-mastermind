from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging.tests.factories import EventFactory
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests import models as test_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ResourceRemovalTest(test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)

    def test_when_virtual_machine_is_deleted_descendant_resources_unlinked(self):
        # Arrange
        vm = factories.TestNewInstanceFactory()
        settings = factories.ServiceSettingsFactory(scope=vm)
        child_vm = factories.TestNewInstanceFactory(service_settings=settings)
        other_vm = factories.TestNewInstanceFactory()

        # Act
        vm.delete()

        # Assert
        self.assertFalse(
            test_models.TestNewInstance.objects.filter(id=child_vm.id).exists()
        )
        self.assertFalse(ServiceSettings.objects.filter(id=settings.id).exists())
        self.assertTrue(
            test_models.TestNewInstance.objects.filter(id=other_vm.id).exists()
        )


class ResourceCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.url = factories.TestNewInstanceFactory.get_list_url()

    def test_shared_key_is_valid_for_virtual_machine_serializer(self):
        shared_key = factories.SshPublicKeyFactory(is_shared=True)
        key_url = factories.SshPublicKeyFactory.get_url(shared_key)

        payload = {
            "ssh_public_key": key_url,
            "service_settings": factories.ServiceSettingsFactory.get_url(
                self.fixture.service_settings
            ),
            "project": factories.ProjectFactory.get_url(self.fixture.project),
            "name": "resource name",
        }

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class ResourceServiceSettingsCustomerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.other_customer = factories.CustomerFactory()
        self.other_customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        self.url = factories.TestNewInstanceFactory.get_list_url()

    def create_resource(self, user, service_settings):
        self.client.force_authenticate(user=user)
        return self.client.post(
            self.url,
            {
                "service_settings": factories.ServiceSettingsFactory.get_url(
                    service_settings
                ),
                "project": factories.ProjectFactory.get_url(self.fixture.project),
                "name": "resource name",
            },
        )

    @data("staff", "owner")
    def test_private_service_settings_of_other_customer_are_rejected(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=False
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            test_models.TestNewInstance.objects.filter(
                service_settings=service_settings
            ).exists()
        )

    @data("staff", "owner")
    def test_shared_service_settings_of_other_customer_are_accepted(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=True
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "owner")
    def test_private_service_settings_of_shared_offering_are_accepted(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=False
        )
        marketplace_factories.OfferingFactory(
            customer=self.other_customer, scope=service_settings, shared=True
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "owner")
    def test_private_service_settings_of_private_offering_are_rejected(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=False
        )
        marketplace_factories.OfferingFactory(
            customer=self.other_customer, scope=service_settings, shared=False
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_private_service_settings_without_customer_are_accepted(self):
        service_settings = factories.ServiceSettingsFactory(customer=None, shared=False)
        response = self.create_resource(self.fixture.staff, service_settings)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "owner")
    def test_private_service_settings_of_project_customer_are_accepted(self, user):
        response = self.create_resource(
            getattr(self.fixture, user), self.fixture.service_settings
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ResourceEventsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.client.force_authenticate(user=self.fixture.staff)
        self.url = factories.TestNewInstanceFactory.get_url(self.fixture.resource)

    def test_filter_events_for_resource_by_scope(self):
        response = self.client.get(EventFactory.get_list_url(), {"scope": self.url})
        self.assertEqual(len(response.data), 1)
