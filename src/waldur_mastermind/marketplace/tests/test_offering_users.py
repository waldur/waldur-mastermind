from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging.models import Event
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.models import OfferingUser, Resource

from . import factories, fixtures


@ddt
class ListOfferingUsersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.secret_options = {
            'service_provider_can_create_offering_user': True
        }
        self.offering.save()
        user = UserFactory()
        self.fixture.project.add_user(user, structure_models.ProjectRole.ADMINISTRATOR)
        OfferingUser.objects.create(offering=self.offering, user=user, username='user')
        user2 = UserFactory()
        offering2 = factories.OfferingFactory(shared=True)
        self.fixture.project.add_user(user, structure_models.ProjectRole.MANAGER)
        OfferingUser.objects.create(offering=offering2, user=user2, username='user2')

    def list_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.get(reverse('marketplace-offering-user-list'))

    @data('owner', 'admin', 'manager')
    def test_authorized_user_can_list_offering_users(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 1)
        self.assertEqual('user', response.data[0]['username'])

    @data('staff', 'global_support')
    def test_authorized_privileged_user_can_list_offering_users(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 2)

    @data(
        'user',
    )
    def test_unauthorized_user_can_not_list_offering_permission(self, user):
        response = self.list_permissions(user)
        self.assertEqual(len(response.data), 0)

    def test_user_can_view_own_offering_user(self):
        sample_user = UserFactory()
        OfferingUser.objects.create(
            offering=self.offering, user=sample_user, username='user3'
        )

        self.client.force_authenticate(sample_user)
        response = self.client.get(reverse('marketplace-offering-user-list'))

        self.assertEqual(1, len(response.data))
        self.assertEqual('user3', response.data[0]['username'])

    def test_user_can_filter_offering_users(self):
        offering_user1 = OfferingUser.objects.get(username='user')
        offering_user1.set_propagation_date()
        offering_user1.save()

        self.client.force_login(self.fixture.staff)

        response = self.client.get(
            reverse('marketplace-offering-user-list'), {'is_not_propagated': False}
        )
        self.assertEqual(1, len(response.data))
        self.assertEqual('user', response.data[0]['username'])
        self.assertIsNotNone(response.data[0]['propagation_date'], response.data[0])


@ddt
class CreateOfferingUsersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.offering.secret_options['service_provider_can_create_offering_user'] = True
        self.offering.save()

    def create_offering_user(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        offering_url = factories.OfferingFactory.get_url(self.offering)
        user_url = UserFactory.get_url(self.fixture.user)
        payload = {'offering': offering_url, 'user': user_url}
        return self.client.post(reverse('marketplace-offering-user-list'), payload)

    @data('staff', 'owner')
    def test_authorized_user_can_create_offering_user(self, user):
        response = self.create_offering_user(user)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)

    @data('staff', 'owner')
    def test_offering_does_not_allow_to_create_user(self, user):
        self.offering.secret_options[
            'service_provider_can_create_offering_user'
        ] = False
        self.offering.save()
        response = self.create_offering_user(user)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_list_offering_permission(self, user):
        response = self.create_offering_user(user)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)


@ddt
class ListUsersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.admin
        self.fixture.manager
        self.fixture.member

        self.url = reverse('user-list')

    @data('service_manager', 'offering_owner')
    def test_user_should_be_able_to_see_users_connected_with_public_resources(
        self, user
    ):
        self.fixture.offering.shared = True
        self.fixture.offering.save()

        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 4)

    @data('service_manager', 'offering_owner')
    def test_user_should_not_be_able_to_see_users_connected_with_private_resources(
        self, user
    ):
        self.fixture.offering.shared = False
        self.fixture.offering.save()
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    @data('service_manager', 'offering_owner', 'user')
    def test_users_related_to_terminated_resources_are_not_exposed(self, user):
        self.fixture.offering.shared = True
        self.fixture.offering.save()

        self.fixture.resource.state = Resource.States.TERMINATED
        self.fixture.resource.save()

        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)


class OfferingUsersHandlerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_when_offering_user_is_created_audit_log_is_generated(self):
        OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.fixture.user,
            username='user',
        )
        self.assertTrue(
            Event.objects.filter(
                event_type='marketplace_offering_user_created'
            ).exists()
        )

    def test_when_offering_user_is_deleted_audit_log_is_generated(self):
        offering_user = OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.fixture.user,
            username='user',
        )
        offering_user.delete()
        self.assertTrue(
            Event.objects.filter(
                event_type='marketplace_offering_user_deleted'
            ).exists()
        )
