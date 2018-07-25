from __future__ import unicode_literals

import mock
from ddt import data, ddt
from rest_framework import status
from rest_framework.test import APITransactionTestCase
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack_tenant import models as openstack_models
from waldur_openstack.openstack_tenant.tests import factories as openstack_factories

from . import factories, fixtures


class JobBaseTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.JobFixture()
        self.job = self.fixture.job

    def _get_valid_payload(self, user, job=None):
        job = job or factories.JobFactory()
        key = structure_factories.SshPublicKeyFactory(user=user)
        return {
            'name': 'test job',
            'service_project_link': openstack_factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
            'ssh_public_key': structure_factories.SshPublicKeyFactory.get_url(key),
            'playbook': factories.PlaybookFactory.get_url(job.playbook),
            'arguments': job.arguments,
        }


@ddt
class JobRetrieveTest(JobBaseTest):

    def test_anonymous_user_cannot_retrieve_job(self):
        response = self.client.get(factories.JobFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'global_support', 'owner',
          'customer_support', 'admin', 'manager', 'project_support')
    def test_user_can_retrieve_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.JobFactory.get_url(self.job))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_user_cannot_retrieve_job(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(factories.JobFactory.get_url(self.job))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class JobCreateTest(JobBaseTest):

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_can_create_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(getattr(self.fixture, user))
        response = self.client.post(factories.JobFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data('global_support', 'customer_support', 'project_support')
    def test_user_cannot_create_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(getattr(self.fixture, user))
        response = self.client.post(factories.JobFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_create_job_with_invalid_argument(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload(self.fixture.staff)
        payload['arguments'] = {'invalid': 'invalid'}

        response = self.client.post(factories.JobFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'], ['Argument invalid is not listed in playbook parameters.'])

    def test_user_cannot_create_job_with_unspecified_required_parameter(self):
        self.client.force_authenticate(self.fixture.staff)
        playbook = factories.PlaybookFactory(
            parameters=[factories.PlaybookParameterFactory(required=True, default='')])
        job = factories.JobFactory(playbook=playbook)
        payload = self._get_valid_payload(self.fixture.staff, job)
        payload['arguments'] = {}

        response = self.client.post(factories.JobFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'], ['Not all required playbook parameters were specified.'])


@ddt
class JobUpdateTest(JobBaseTest):

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_can_update_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {'name': 'test job 2'}
        response = self.client.put(factories.JobFactory.get_url(self.job), data=payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.job.refresh_from_db()
        self.assertEqual(self.job.name, payload['name'])

    @data('global_support', 'customer_support', 'project_support')
    def test_user_cannot_update_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {'name': 'test job 2'}
        response = self.client.put(factories.JobFactory.get_url(self.job), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class JobDeleteTest(JobBaseTest):

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_user_can_delete_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(factories.JobFactory.get_url(self.job))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('global_support', 'customer_support', 'project_support')
    def test_non_authorized_user_cannot_delete_job(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(factories.JobFactory.get_url(self.job))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_if_related_resources_are_not_stable_deletion_is_not_allowed(self):
        vm = openstack_factories.InstanceFactory()
        vm.tags.add(self.job.get_tag())
        vm.state = openstack_models.Instance.States.UPDATING
        vm.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(factories.JobFactory.get_url(self.job))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class CountersTest(JobBaseTest):
    def test_project_counter_has_experts(self):
        url = structure_factories.ProjectFactory.get_url(self.fixture.project, action='counters')
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(url, {'fields': ['ansible']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'ansible': 1})


class JobBackendTest(JobBaseTest):
    @mock.patch('subprocess.check_output')
    @mock.patch('os.path.exists')
    def test_job_id_is_passed_as_extra_argument_to_ansible(self, path_exists, check_output):
        path_exists.return_value = True
        check_output.return_value = 'OK'

        self.job.get_backend().run_job(self.job)
        args = check_output.call_args[0][0]
        command = ' '.join(args)
        self.assertTrue(self.job.get_tag() in command)
