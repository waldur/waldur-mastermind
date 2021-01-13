import unittest

from rest_framework import status, test

from waldur_core.core import models as core_models
from waldur_core.logging.tests.factories import EventFactory
from waldur_core.structure.models import NewResource, ServiceSettings
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests import models as test_models

States = core_models.StateMixin.States


class ResourceRemovalTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)

    @unittest.skip('Unlink operation is not supported for new style resources yet.')
    def test_vm_unlinked_immediately_anyway(self):
        vm = factories.TestNewInstanceFactory(state=States.UPDATING)
        url = factories.TestNewInstanceFactory.get_url(vm, 'unlink')
        response = self.client.post(url)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

    @unittest.skip('Unlink operation is not supported for new style resources yet.')
    def test_new_resource_unlinked_immediately(self):
        vm = factories.TestNewInstanceFactory(state=NewResource.States.OK)
        url = factories.TestNewInstanceFactory.get_url(vm, 'unlink')

        response = self.client.post(url)

        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

    def test_when_virtual_machine_is_deleted_descendant_resources_unlinked(self):
        # Arrange
        vm = factories.TestNewInstanceFactory()
        settings = factories.ServiceSettingsFactory(scope=vm)
        service = factories.TestServiceFactory(settings=settings)
        link = factories.TestServiceProjectLinkFactory(service=service)
        child_vm = factories.TestNewInstanceFactory(service_project_link=link)
        other_vm = factories.TestNewInstanceFactory()

        # Act
        vm.delete()

        # Assert
        self.assertFalse(
            test_models.TestNewInstance.objects.filter(id=child_vm.id).exists()
        )
        self.assertFalse(test_models.TestService.objects.filter(id=service.id).exists())
        self.assertFalse(ServiceSettings.objects.filter(id=settings.id).exists())
        self.assertTrue(
            test_models.TestNewInstance.objects.filter(id=other_vm.id).exists()
        )


class ResourceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()

    def test_shared_key_is_valid_for_virtual_machine_serializer(self):
        shared_key = factories.SshPublicKeyFactory(is_shared=True)
        key_url = factories.SshPublicKeyFactory.get_url(shared_key)

        spl_url = factories.TestServiceProjectLinkFactory.get_url(
            self.fixture.service_project_link
        )
        payload = {
            'service_project_link': spl_url,
            'name': 'valid name',
            'ssh_public_key': key_url,
        }
        url = factories.TestNewInstanceFactory.get_list_url()

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_may_specify_service_settings_and_project_instead_of_service_project_link(
        self,
    ):
        payload = {
            'service_settings': factories.ServiceSettingsFactory.get_url(
                self.fixture.service_settings
            ),
            'project': factories.ProjectFactory.get_url(self.fixture.project),
            'name': 'resource name',
        }
        # Create SPL so that resource provision succeeds
        spl_url = factories.TestServiceProjectLinkFactory.get_url(
            self.fixture.service_project_link
        )

        url = factories.TestNewInstanceFactory.get_list_url()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(spl_url, response.data['service_project_link'])

    def test_resource_provision_fails_if_matching_service_project_link_does_not_exist(
        self,
    ):
        # Do not create SPL so that resource provision fails
        payload = {
            'service_settings': factories.ServiceSettingsFactory.get_url(
                self.fixture.service_settings
            ),
            'project': factories.ProjectFactory.get_url(self.fixture.project),
            'name': 'resource name',
        }

        url = factories.TestNewInstanceFactory.get_list_url()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResourceTagsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.client.force_authenticate(user=self.fixture.staff)

    def test_resource_tags_are_rendered_as_list(self):
        self.fixture.resource.tags.add('tag1')
        self.fixture.resource.tags.add('tag2')

        url = factories.TestNewInstanceFactory.get_url(self.fixture.resource)
        response = self.client.get(url)
        self.assertEqual(response.data['tags'], ['tag1', 'tag2'])

    def test_tags_are_saved_on_resource_provision(self):
        payload = {
            'service_project_link': factories.TestServiceProjectLinkFactory.get_url(
                self.fixture.service_project_link
            ),
            'name': 'Tagged resource',
            'tags': ['tag1', 'tag2'],
        }
        url = factories.TestNewInstanceFactory.get_list_url()

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(set(response.data['tags']), {'tag1', 'tag2'})

    def test_tags_are_saved_on_resource_modification(self):
        resource = self.fixture.resource
        resource.state = test_models.TestNewInstance.States.OK
        resource.save()
        resource.tags.add('tag1')
        resource.tags.add('tag2')
        payload = {'tags': []}
        url = factories.TestNewInstanceFactory.get_url(resource)

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['tags'], [])

    def test_resource_can_be_filtered_by_tag(self):
        self.fixture.resource.tags.add('tag1')
        resource2 = factories.TestNewInstanceFactory(
            service_project_link=self.fixture.service_project_link
        )
        resource2.tags.add('tag2')

        url = factories.TestNewInstanceFactory.get_list_url()
        response = self.client.get(url, {'tag': 'tag1'})
        self.assertEqual(len(response.data), 1)


class ResourceEventsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.client.force_authenticate(user=self.fixture.staff)
        self.url = factories.TestNewInstanceFactory.get_url(self.fixture.resource)

    def test_filter_events_for_resource_by_scope(self):
        response = self.client.get(EventFactory.get_list_url(), {'scope': self.url})
        self.assertEqual(len(response.data), 1)
