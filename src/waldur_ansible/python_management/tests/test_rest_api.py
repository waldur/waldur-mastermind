from __future__ import unicode_literals

import uuid

from ddt import data, ddt
from rest_framework import status
from rest_framework.test import APITransactionTestCase
from waldur_openstack.openstack_tenant.tests import factories as openstack_factories

from . import factories, fixtures


class PythonManagementBaseTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PythonManagementFixture()
        self.python_management = self.fixture.python_management

    def _get_valid_payload(self, python_management):
        return {
            'service_project_link': openstack_factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
            'instance': openstack_factories.InstanceFactory.get_url(self.fixture.instance),
            'virtual_envs_dir_path': uuid.uuid4().hex,
            'virtual_environments': []
        }


@ddt
class PythonManagementRetrieveTest(PythonManagementBaseTest):

    def test_anonymous_user_cannot_retrieve_job(self):
        response = self.client.get(factories.PythonManagementFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'global_support', 'owner',
          'customer_support', 'admin', 'manager', 'project_support')
    def test_user_can_retrieve_python_management(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.PythonManagementFactory.get_url(self.python_management))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


@ddt
class PythonManagementCreateTest(PythonManagementBaseTest):

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_can_create_python_management(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(self.fixture.python_management)

        response = self.client.post(factories.PythonManagementFactory.get_list_url(), data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data('global_support', 'customer_support', 'project_support')
    def test_user_cannot_create_python_management(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(self.fixture.python_management)

        response = self.client.post(factories.PythonManagementFactory.get_list_url(), data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
