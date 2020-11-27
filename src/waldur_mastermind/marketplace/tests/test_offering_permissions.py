from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

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


@ddt
class RevokeOfferingPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.permission = OfferingPermission.objects.create(
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
