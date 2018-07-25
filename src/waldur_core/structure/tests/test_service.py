from ddt import ddt, data
from django.db import models
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.models import ProjectRole
from waldur_core.structure.tests import factories, fixtures, models as test_models


@ddt
class ServiceListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.service = self.fixture.service
        self.spl = self.fixture.service_project_link

    def get_list(self):
        return self.client.get(factories.TestServiceFactory.get_list_url())

    def test_anonymous_user_cannot_list_services(self):
        response = self.get_list()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_list_services(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.get_list()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        service_url = factories.TestServiceFactory.get_url(self.service)
        self.assertIn(service_url, [instance['url'] for instance in response.data])

    def test_user_cannot_list_services_of_projects_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.fixture.user)

        response = self.get_list()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, len(response.data))


@ddt
class ServiceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.TestServiceFactory.get_list_url()

    def test_if_required_fields_are_specified_service_is_created(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_if_required_field_is_not_specified_error_raised(self):
        self.client.force_authenticate(self.fixture.owner)

        payload = self._get_valid_payload()
        del payload['username']
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('username', response.data)

    @data('staff', 'owner')
    def test_staff_and_owner_can_create_service(self, user):
        self.assert_user_can_create_service(user)

    @data('admin', 'manager')
    def test_admin_and_manager_can_not_create_service(self, user):
        self.assert_user_can_not_create_service(user)

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_he_can_create_it(self):
        self.assert_user_can_create_service('staff')

    @data('owner', 'admin', 'manager')
    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_other_users_can_not_create_it(self, user):
        self.assert_user_can_not_create_service(user)

    def assert_user_can_create_service(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def assert_user_can_not_create_service(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def _get_valid_payload(self):
        customer_url = factories.CustomerFactory.get_url(self.fixture.customer)
        return {
            'name': 'Test service',
            'customer': customer_url,
            'backend_url': 'http://example.com/',
            'username': 'admin',
            'password': 'secret',
        }


@ddt
class ServiceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.service = self.fixture.service
        self.url = factories.TestServiceFactory.get_url(self.service)

    @data('staff', 'owner')
    def test_staff_and_owner_can_delete_service(self, user):
        self.assert_user_can_delete_service(user)

    @data('admin', 'manager')
    def test_admin_and_manager_can_not_delete_service(self, user):
        self.assert_user_can_not_delete_service(user, status.HTTP_404_NOT_FOUND)

    @data('staff')
    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_he_can_delete_it(self, user):
        self.assert_user_can_delete_service(user)

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_owner_can_not_delete_it(self):
        self.assert_user_can_not_delete_service('owner', status.HTTP_403_FORBIDDEN)

    def assert_user_can_delete_service(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(test_models.TestService.objects.filter(pk=self.service.pk).exists())

    def assert_user_can_not_delete_service(self, user, expected_status):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, expected_status)
        self.assertTrue(test_models.TestService.objects.filter(pk=self.service.pk).exists())


class ServiceResourcesCounterTest(test.APITransactionTestCase):
    """
    There's one shared service. Also there are 2 users each of which has one project.
    There's one VM in each project. Service counters for each user should equal 1.
    For staff user resource counter should equal 2.
    """

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.settings = factories.ServiceSettingsFactory(shared=True)
        self.service = factories.TestServiceFactory(customer=self.customer, settings=self.settings)

        self.user1 = factories.UserFactory()
        self.project1 = factories.ProjectFactory(customer=self.customer)
        self.project1.add_user(self.user1, ProjectRole.ADMINISTRATOR)
        self.spl1 = factories.TestServiceProjectLinkFactory(service=self.service, project=self.project1)
        self.vm1 = factories.TestNewInstanceFactory(service_project_link=self.spl1)

        self.user2 = factories.UserFactory()
        self.project2 = factories.ProjectFactory(customer=self.customer)
        self.project2.add_user(self.user2, ProjectRole.ADMINISTRATOR)
        self.spl2 = factories.TestServiceProjectLinkFactory(service=self.service, project=self.project2)
        self.vm2 = factories.TestNewInstanceFactory(service_project_link=self.spl2)

        self.service_url = factories.TestServiceFactory.get_url(self.service)

    def test_counters_for_shared_providers_should_be_filtered_by_user(self):
        self.client.force_authenticate(self.user1)
        response = self.client.get(self.service_url)
        self.assertEqual(1, response.data['resources_count'])

        self.client.force_authenticate(self.user2)
        response = self.client.get(self.service_url)
        self.assertEqual(1, response.data['resources_count'])

    def test_counters_are_not_filtered_for_staff(self):
        self.client.force_authenticate(factories.UserFactory(is_staff=True))
        response = self.client.get(self.service_url)
        self.assertEqual(2, response.data['resources_count'])

    def test_subresources_are_skipped(self):
        factories.TestSubResourceFactory(service_project_link=self.spl1)
        self.client.force_authenticate(self.user1)
        response = self.client.get(self.service_url)
        self.assertEqual(1, response.data['resources_count'])


class ServiceUnlinkTest(test.APITransactionTestCase):
    def test_when_service_is_unlinked_all_related_resources_are_unlinked_too(self):
        resource = factories.TestNewInstanceFactory()
        service = resource.service_project_link.service
        unlink_url = factories.TestServiceFactory.get_url(service, 'unlink')

        self.client.force_authenticate(factories.UserFactory(is_staff=True))
        response = self.client.post(unlink_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertRaises(models.ObjectDoesNotExist, service.refresh_from_db)

    def test_owner_can_unlink_managed_service(self):
        fixture = fixtures.ServiceFixture()
        service = fixture.service

        unlink_url = factories.TestServiceFactory.get_url(service, 'unlink')
        self.client.force_authenticate(fixture.owner)
        response = self.client.post(unlink_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertRaises(models.ObjectDoesNotExist, service.refresh_from_db)

    def test_owner_cannot_unlink_service_with_shared_settings(self):
        fixture = fixtures.ServiceFixture()
        service_settings = factories.ServiceSettingsFactory(shared=True)
        service = test_models.TestService.objects.get(customer=fixture.customer, settings=service_settings)
        unlink_url = factories.TestServiceFactory.get_url(service, 'unlink')
        self.client.force_authenticate(fixture.owner)

        response = self.client.post(unlink_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(test_models.TestService.objects.filter(pk=service.pk).exists())

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_owner_cannot_unlink_it(self):
        fixture = fixtures.ServiceFixture()
        service = fixture.service

        unlink_url = factories.TestServiceFactory.get_url(service, 'unlink')
        self.client.force_authenticate(fixture.owner)
        response = self.client.post(unlink_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(test_models.TestService.objects.filter(pk=service.pk).exists())
