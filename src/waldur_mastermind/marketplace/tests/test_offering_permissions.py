from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.utils import (
    client_add_user,
    client_delete_user,
    client_list_users,
    client_update_user,
)
from waldur_mastermind.marketplace import models

from . import factories


class BaseOfferingPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING_PERMISSION)


@ddt
class ListOfferingPermissionTest(BaseOfferingPermissionTest):
    def setUp(self):
        super().setUp()
        self.offering.add_user(self.fixture.user, OfferingRole.MANAGER)

    def list_users(self, user):
        return client_list_users(
            self.client, getattr(self.fixture, user), self.offering
        )

    @data('staff', 'owner')
    def test_authorized_user_can_list_offering_permission(self, user):
        response = self.list_users(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_list_offering_permission(self, user):
        response = self.list_users(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class GrantOfferingPermissionTest(BaseOfferingPermissionTest):
    def grant_permission(self, user):
        return client_add_user(
            self.client,
            getattr(self.fixture, user),
            self.fixture.user,
            self.offering,
            OfferingRole.MANAGER,
        )

    @data('staff', 'owner')
    def test_authorized_user_can_grant_offering_permission(self, user):
        response = self.grant_permission(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_grant_offering_permission(self, user):
        response = self.grant_permission(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('staff', 'owner')
    def test_authorized_user_can_not_grant_permission_for_private_offering(self, user):
        self.offering.shared = False
        self.offering.save(update_fields=['shared'])
        response = self.grant_permission(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_offering_permission_is_granted_customer_permission_is_granted_too(
        self,
    ):
        self.grant_permission('owner')
        self.assertTrue(
            self.offering.customer.has_user(self.fixture.user, CustomerRole.MANAGER)
        )

    def test_service_manager_permission_is_created_even_for_customer_owner(
        self,
    ):
        self.offering.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        self.grant_permission('owner')
        self.assertTrue(
            self.offering.customer.has_user(self.fixture.user, CustomerRole.MANAGER)
        )


@ddt
@freeze_time('2020-01-01')
class UpdateOfferingPermissionTest(BaseOfferingPermissionTest):
    def setUp(self):
        super().setUp()
        self.target_user = UserFactory()
        self.offering.add_user(self.target_user, OfferingRole.MANAGER)

    def change_permission(self, user):
        return client_update_user(
            self.client,
            getattr(self.fixture, user),
            self.target_user,
            self.offering,
            OfferingRole.MANAGER,
            '2021-01-01T00:00',
        )

    @data('staff', 'owner')
    def test_authorized_user_can_change_offering_permission(self, user):
        response = self.change_permission(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_change_offering_permission(self, user):
        response = self.change_permission(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RevokeOfferingPermissionTest(BaseOfferingPermissionTest):
    def setUp(self):
        super().setUp()
        self.offering.add_user(self.fixture.user, OfferingRole.MANAGER)

    def revoke_permission(self, user):
        return client_delete_user(
            self.client,
            getattr(self.fixture, user),
            self.fixture.user,
            self.offering,
            OfferingRole.MANAGER,
        )

    @data('staff', 'owner')
    def test_authorized_user_can_revoke_offering_permission(self, user):
        response = self.revoke_permission(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unauthorized_user_can_not_revoke_offering_permission(self):
        response = self.revoke_permission('admin')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_when_offering_permission_is_revoked_customer_permission_is_revoked_too(
        self,
    ):
        self.revoke_permission('owner')
        self.assertFalse(
            self.offering.customer.has_user(self.fixture.user, CustomerRole.MANAGER)
        )

    def test_customer_permission_is_not_revoked_if_another_offering_exists(
        self,
    ):
        offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        self.revoke_permission('owner')
        self.assertTrue(
            self.fixture.customer.has_user(self.fixture.user, CustomerRole.MANAGER)
        )

    def test_when_service_manager_role_is_revoked_offering_permissions_are_revoked_too(
        self,
    ):
        self.offering.customer.remove_user(self.fixture.user, CustomerRole.MANAGER)
        self.assertFalse(self.offering.has_user(self.fixture.user))


@ddt
class OfferingUpdateTest(BaseOfferingPermissionTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingFactory.get_url(self.offering)
        self.offering.state = models.Offering.States.DRAFT
        self.offering.save()
        self.offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    def test_service_manager_can_update_offering_in_draft_state(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, 'new_offering')

    def test_offering_lookup_succeeds_if_more_than_one_manager_exists(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data(
        models.Offering.States.ACTIVE,
        models.Offering.States.PAUSED,
        models.Offering.States.ARCHIVED,
    )
    def test_service_manager_can_not_update_offering_in_active_or_paused_state(
        self, state
    ):
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.user)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
