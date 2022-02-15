import mock
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import PLUGIN_NAME
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_azure import VIRTUAL_MACHINE_TYPE


@ddt
class ImportableOfferingsListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()

    def list_offerings(self, shared, user, project=None, type=VIRTUAL_MACHINE_TYPE):
        factories.OfferingFactory(
            scope=self.fixture.service_settings,
            shared=shared,
            customer=self.fixture.customer,
            project=project,
            type=type,
        )
        list_url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.get(list_url, {'importable': True}).data

    def test_if_plugin_does_not_support_import_related_offering_is_filtered_out(self):
        offerings = self.list_offerings(shared=True, user='staff', type=PLUGIN_NAME)
        self.assertEqual(0, len(offerings))

    def test_staff_can_list_importable_shared_offerings(self):
        offerings = self.list_offerings(shared=True, user='staff')
        self.assertEqual(1, len(offerings))

    @data('owner', 'manager', 'admin')
    def test_other_users_can_not_list_importable_shared_offerings(self, user):
        offerings = self.list_offerings(shared=True, user=user)
        self.assertEqual(0, len(offerings))

    @data(
        'staff',
        'owner',
    )
    def test_staff_and_owner_can_list_importable_private_offerings(self, user):
        offerings = self.list_offerings(shared=False, user=user)
        self.assertEqual(1, len(offerings))

    @data('staff', 'owner', 'manager', 'admin')
    def test_project_users_can_list_importable_private_offerings_if_they_have_relation_with_project(
        self, user
    ):
        self.fixture.manager
        self.fixture.service_settings.scope = self.fixture.resource
        self.fixture.service_settings.save()
        offerings = self.list_offerings(
            shared=False, user=user, project=self.fixture.project
        )
        self.assertEqual(1, len(offerings))


class ImportableResourcesListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()

        self.mock_method = mock.patch(
            'waldur_mastermind.marketplace.plugins.manager.get_importable_resources_backend_method'
        ).start()
        self.mock_method.return_value = 'get_importable_virtual_machines'

        self.mock_backend = mock.patch(
            'waldur_core.structure.models.ServiceSettings.get_backend'
        ).start()
        self.mock_backend().get_importable_virtual_machines.return_value = []

    def tearDown(self):
        super(ImportableResourcesListTest, self).tearDown()
        mock.patch.stopall()

    def list_resources(self, shared, user, project=None):
        offering = factories.OfferingFactory(
            scope=self.fixture.service_settings,
            shared=shared,
            customer=self.fixture.customer,
            project=project,
        )
        list_url = factories.OfferingFactory.get_url(offering, 'importable_resources')
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.get(list_url)

    def test_staff_can_list_importable_resources_from_shared_offering(self):
        response = self.list_resources(shared=True, user='staff')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_not_list_importable_resources_from_shared_offering(self):
        response = self.list_resources(shared=True, user='owner')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_list_importable_resources_from_private_offering(self):
        response = self.list_resources(shared=False, user='owner')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_manager_cannot_list_importable_resources(self):
        response = self.list_resources(
            shared=False, user='manager', project=self.fixture.project
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_another_owner_can_not_list_importable_resources(self):
        # Arrange
        offering = factories.OfferingFactory(
            scope=self.fixture.service_settings,
            shared=False,
            type='Test.VirtualMachine',
        )

        # Act
        list_url = factories.OfferingFactory.get_url(offering, 'importable_resources')
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(list_url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_if_plugin_does_not_support_resource_import_validation_error_is_raised(
        self,
    ):
        mock.patch.stopall()
        offering = factories.OfferingFactory(
            scope=self.fixture.service_settings,
            shared=False,
            type='Test.VirtualMachine',
            customer=self.fixture.customer,
        )
        list_url = factories.OfferingFactory.get_url(offering, 'importable_resources')
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(list_url)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
