from ddt import data, ddt
from rest_framework import test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.models import OfferingUser

from . import factories


@ddt
class ListOfferingUsersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.user, username='user'
        )

    def list_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.get(reverse('marketplace-offering-user-list'))

    @data('staff', 'global_support', 'owner', 'user')
    def test_authorized_user_can_list_offering_users(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 1)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_list_offering_permission(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 0)
