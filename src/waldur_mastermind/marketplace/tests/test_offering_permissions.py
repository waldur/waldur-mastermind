from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.models import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.models import OfferingPermission

from . import factories


@ddt
class ListOfferingPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        OfferingPermission.objects.create(
            offering=self.offering, user=self.fixture.user, is_active=True
        )

    def list_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.get(reverse('marketplace-offering-permission-list'))

    @data('staff', 'owner')
    def test_authorized_user_can_list_offering_permission(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 1)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_list_offering_permission(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 0)


@ddt
class GrantOfferingPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )

    def grant_permission(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.post(
            reverse('marketplace-offering-permission-list'),
            {
                'offering': factories.OfferingFactory.get_url(self.offering),
                'user': UserFactory.get_url(self.fixture.user),
            },
        )

    @data('staff', 'owner')
    def test_authorized_user_can_grant_offering_permission(self, user):
        response = self.grant_permission(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_grant_offering_permission(self, user):
        response = self.grant_permission(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('staff', 'owner')
    def test_authorized_user_can_not_grant_permission_for_private_offering(self, user):
        self.offering.shared = False
        self.offering.save(update_fields=['shared'])
        response = self.grant_permission(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_when_offering_permission_is_granted_customer_permission_is_granted_too(
        self,
    ):
        self.grant_permission('owner')
        self.assertTrue(
            self.offering.customer.has_user(
                self.fixture.user, CustomerRole.SERVICE_MANAGER
            )
        )

    def test_service_manager_permission_is_created_even_for_customer_owner(self,):
        self.offering.customer.add_user(self.fixture.user, CustomerRole.OWNER)
        self.grant_permission('owner')
        self.assertTrue(
            self.offering.customer.has_user(
                self.fixture.user, CustomerRole.SERVICE_MANAGER
            )
        )


@ddt
class RevokeOfferingPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.add_user(self.fixture.user)
        self.permission = OfferingPermission.objects.get(
            offering=self.offering, user=self.fixture.user, is_active=True
        )

    def revoke_permission(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.delete(
            reverse(
                'marketplace-offering-permission-detail',
                kwargs={'pk': self.permission.pk},
            )
        )

    @data('staff', 'owner')
    def test_authorized_user_can_revoke_offering_permission(self, user):
        response = self.revoke_permission(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthorized_user_can_not_revoke_offering_permission(self):
        response = self.revoke_permission('admin')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_when_offering_permission_is_revoked_customer_permission_is_revoked_too(
        self,
    ):
        self.revoke_permission('owner')
        self.assertFalse(
            self.offering.customer.has_user(
                self.fixture.user, CustomerRole.SERVICE_MANAGER
            )
        )

    def test_customer_permission_is_not_revoked_if_another_offering_exists(self,):
        offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        offering.add_user(self.fixture.user)
        self.revoke_permission('owner')
        self.assertTrue(
            self.fixture.customer.has_user(
                self.fixture.user, CustomerRole.SERVICE_MANAGER
            )
        )

    def test_when_service_manager_role_is_revoked_offering_permissions_are_revoked_too(
        self,
    ):
        self.offering.customer.remove_user(
            self.fixture.user, CustomerRole.SERVICE_MANAGER
        )
        self.assertFalse(self.offering.has_user(self.fixture.user,))


@ddt
class OfferingUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.url = factories.OfferingFactory.get_url(self.offering)
        self.permission = OfferingPermission.objects.create(
            offering=self.offering, user=self.fixture.user, is_active=True
        )

    def test_service_manager_can_update_offering_in_draft_state(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, 'new_offering')

    def test_offering_lookup_succeeds_if_more_than_one_manager_exists(self):
        self.client.force_authenticate(self.fixture.user)
        user = UserFactory()
        OfferingPermission.objects.create(
            offering=self.offering, user=user, is_active=True
        )
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
