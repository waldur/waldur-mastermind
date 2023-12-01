from ddt import data, ddt
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ManagerGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call_manager = self.fixture.manager

    @data(
        'staff',
        'owner',
        'user',
        'customer_support',
    )
    def test_manager_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_manager_should_be_visible_to_unauthenticated_users_by_default(
        self,
    ):
        url = factories.CallManagingOrganisationFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_manager_should_be_invisible_to_unauthenticated_users_when_offerings_are_public(
        self,
    ):
        url = factories.CallManagingOrganisationFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class ManagerCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff')
    def test_staff_can_create_manager(self, user):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_manager(self, user):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_create_manager_with_settings_enabled(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.REGISTER_SERVICE_PROVIDER)
        response = self.create_manager('owner')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('owner')
    def test_owner_can_not_create_manager_with_settings_disabled(self, user):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_manager_with_settings_disabled(
        self, user
    ):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_manager(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
        }

        return self.client.post(url, payload)


@ddt
class ManagerUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_manager(self, user):
        response, call_manager = self.update_manager(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        call_manager.refresh_from_db()
        self.assertEqual(call_manager.description, 'new description')
        self.assertTrue(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_manager(self, user):
        response, call_manager = self.update_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_manager(self, user, payload=None, **kwargs):
        if not payload:
            payload = {'description': 'new description'}

        call_manager = factories.CallManagingOrganisationFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_url(call_manager)

        response = self.client.patch(url, payload, **kwargs)
        call_manager.refresh_from_db()

        return response, call_manager

    def test_upload_image(self):
        payload = {'image': dummy_image()}
        response, call_manager = self.update_manager(
            'staff', payload=payload, format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(call_manager.image)

        url = factories.CallManagingOrganisationFactory.get_url(call_manager)
        response = self.client.patch(url, {'image': None})
        call_manager.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(call_manager.image)


@ddt
class ManagerDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.call_manager = factories.CallManagingOrganisationFactory(
            customer=self.customer
        )

    @data('staff', 'owner')
    def test_authorized_user_can_delete_manager(self, user):
        response = self.delete_manager(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_manager(self, user):
        response = self.delete_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    def delete_manager(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_url(self.call_manager)
        response = self.client.delete(url)
        return response
