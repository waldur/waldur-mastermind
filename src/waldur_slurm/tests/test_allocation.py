from __future__ import unicode_literals

from ddt import ddt, data
import mock
from rest_framework import status, test

from waldur_freeipa import models as freeipa_models

from . import factories, fixtures


class AllocationGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation)

    def test_freeipa_username_is_returned_if_profile_exists(self):
        freeipa_models.Profile.objects.create(user=self.fixture.admin, username='waldur_admin')
        self.client.force_login(self.fixture.admin)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'waldur_admin')

    def test_gateway_is_returned_if_is_defined(self):
        settings = self.fixture.service.settings
        settings.options['gateway'] = '8.8.8.8'
        settings.save()

        self.client.force_login(self.fixture.admin)

        response = self.client.get(self.url)
        self.assertEqual(response.data['gateway'], '8.8.8.8')

    def test_hostname_is_returned_if_is_defined(self):
        settings = self.fixture.service.settings
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

    @data('owner', 'staff')
    def test_authorized_user_can_create_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.assertEqual(response.data['cpu_limit'], 100)
        self.assertEqual(response.data['gpu_limit'], 200)
        self.assertEqual(response.data['ram_limit'], 300)

    @data('admin', 'manager')
    def test_non_authorized_user_can_not_create_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def get_valid_payload(self):
        return {
            'name': 'Test allocation',
            'service_project_link': factories.SlurmServiceProjectLinkFactory.get_url(self.fixture.spl),
            'cpu_limit': 100,
            'gpu_limit': 200,
            'ram_limit': 300,
        }


@ddt
class AllocationDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation)

    @data('staff')
    def test_authorized_user_can_delete_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('owner', 'admin', 'manager')
    def test_non_authorized_user_can_not_delete_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AllocationCancelTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation, 'cancel')

    @data('staff', 'owner')
    def test_authorized_user_can_cancel_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        with mock.patch('subprocess.check_output'):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('admin', 'manager')
    def test_non_authorized_user_can_not_cancel_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        with mock.patch('subprocess.check_output'):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AllocationUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.url = factories.AllocationFactory.get_url(self.fixture.allocation)

    @data('owner', 'staff')
    def test_authorized_user_can_update_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.patch(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['cpu_limit'], 100)
        self.assertEqual(response.data['gpu_limit'], 200)
        self.assertEqual(response.data['ram_limit'], 300)

    @data('admin', 'manager')
    def test_non_authorized_user_can_not_update_allocation(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.client.patch(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def get_valid_payload(self):
        return {
            'cpu_limit': 100,
            'gpu_limit': 200,
            'ram_limit': 300,
        }
