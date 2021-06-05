from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests.factories import ProjectFactory
from waldur_freeipa import models as freeipa_models
from waldur_slurm.tests.factories import SlurmServiceSettingsFactory

from . import factories, fixtures


class AllocationGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation)

    def test_freeipa_username_is_returned_if_profile_exists(self):
        freeipa_models.Profile.objects.create(
            user=self.fixture.admin, username='waldur_admin'
        )
        self.client.force_login(self.fixture.admin)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'waldur_admin')

    def test_gateway_is_returned_if_is_defined(self):
        settings = self.fixture.settings
        settings.options['gateway'] = '8.8.8.8'
        settings.save()

        self.client.force_login(self.fixture.admin)

        response = self.client.get(self.url)
        self.assertEqual(response.data['gateway'], '8.8.8.8')

    def test_hostname_is_returned_if_is_defined(self):
        settings = self.fixture.settings
        settings.options['hostname'] = '4.4.4.4'
        settings.save()

        self.client.force_login(self.fixture.admin)

        response = self.client.get(self.url)
        self.assertEqual(response.data['gateway'], '4.4.4.4')


@ddt
class AllocationCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_list_url()

    @data('owner', 'staff', 'admin', 'manager')
    def test_authorized_user_can_create_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data('member',)
    def test_non_authorized_user_can_not_create_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def get_valid_payload(self):
        return {
            'name': 'Test-allocation',
            'service_settings': SlurmServiceSettingsFactory.get_url(
                self.fixture.settings
            ),
            'project': ProjectFactory.get_url(self.fixture.project),
        }


@ddt
class AllocationDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_delete_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('member')
    def test_non_authorized_user_can_not_delete_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AllocationUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_update_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.patch(self.url, {'description': 'New description.'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('member')
    def test_non_authorized_user_can_not_update_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.patch(self.url, {'description': 'New description.'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AllocationCancelTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(
            self.fixture.allocation, 'cancel'
        )


@ddt
class AllocationSetLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.allocation = self.fixture.allocation
        self.url = factories.AllocationFactory.get_url(self.allocation, 'set_limits')

    def test_authorized_user_can_update_allocation(self):
        self.client.force_login(self.fixture.staff)

        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.allocation.refresh_from_db()
        self.assertEqual(100, self.allocation.cpu_limit)
        self.assertEqual(200, self.allocation.gpu_limit)
        self.assertEqual(300, self.allocation.ram_limit)

    def test_user_can_not_update_allocation_with_invalid_limits(self):
        self.client.force_login(self.fixture.staff)
        payload = self.get_valid_payload()
        payload['cpu_limit'] = -2

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('owner', 'admin', 'manager')
    def test_non_authorized_user_can_not_update_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def get_valid_payload(self):
        return {
            'cpu_limit': 100,
            'gpu_limit': 200,
            'ram_limit': 300,
        }
