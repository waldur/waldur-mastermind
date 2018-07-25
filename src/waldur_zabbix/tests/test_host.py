import mock

from django.test import TestCase
import pyzabbix
from requests import RequestException
from rest_framework import status, test

from waldur_core.structure import ServiceBackendError
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .. import models
from ..apps import ZabbixConfig


class HostApiCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.spl = factories.ZabbixServiceProjectLinkFactory()

    def test_visible_name_populated_from_scope(self):
        vm = structure_factories.TestNewInstanceFactory()
        data = {
            'service_project_link': factories.ZabbixServiceProjectLinkFactory.get_url(self.spl),
            'name': 'Valid host name',
            'scope': structure_factories.TestNewInstanceFactory.get_url(vm)
        }
        response = self.client.post(factories.HostFactory.get_list_url(), data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['visible_name'], models.Host.get_visible_name_from_scope(vm))

    def test_visible_name_should_be_unique(self):
        factories.HostFactory(service_project_link=self.spl, visible_name='Unique visible host name')
        response = self.client.post(factories.HostFactory.get_list_url(), {
            'service_project_link': factories.ZabbixServiceProjectLinkFactory.get_url(self.spl),
            'name': 'Valid host name',
            'visible_name': 'Unique visible host name'
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_impossible_to_add_child_template_to_host(self):
        template = factories.TemplateFactory(settings=self.spl.service.settings)
        child_template = factories.TemplateFactory(settings=self.spl.service.settings)
        template.children.add(child_template)

        response = self.client.post(factories.HostFactory.get_list_url(), {
            'service_project_link': factories.ZabbixServiceProjectLinkFactory.get_url(self.spl),
            'name': 'Valid host name',
            'visible_name': 'Visible name',
            'templates': [
                {'url': factories.TemplateFactory.get_url(template)},
                {'url': factories.TemplateFactory.get_url(child_template)},
            ]
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_impossible_to_add_parent_template_to_host(self):
        template = factories.TemplateFactory(settings=self.spl.service.settings)
        parent_template = factories.TemplateFactory(settings=self.spl.service.settings)
        template.parents.add(parent_template)

        response = self.client.post(factories.HostFactory.get_list_url(), {
            'service_project_link': factories.ZabbixServiceProjectLinkFactory.get_url(self.spl),
            'name': 'Valid host name',
            'visible_name': 'Visible name',
            'templates': [
                {'url': factories.TemplateFactory.get_url(template)},
                {'url': factories.TemplateFactory.get_url(parent_template)},
            ]
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_impossible_to_add_templates_to_host_with_common_parent(self):
        template1 = factories.TemplateFactory(settings=self.spl.service.settings)
        template2 = factories.TemplateFactory(settings=self.spl.service.settings)
        parent_template = factories.TemplateFactory(settings=self.spl.service.settings)
        template1.parents.add(parent_template)
        template2.parents.add(parent_template)

        response = self.client.post(factories.HostFactory.get_list_url(), {
            'service_project_link': factories.ZabbixServiceProjectLinkFactory.get_url(self.spl),
            'name': 'Valid host name',
            'visible_name': 'Visible name',
            'templates': [
                {'url': factories.TemplateFactory.get_url(template1)},
                {'url': factories.TemplateFactory.get_url(template2)},
            ]
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class HostCreateBackendTest(TestCase):
    def setUp(self):
        self.patcher = mock.patch('pyzabbix.ZabbixAPI')
        self.mocked_api = self.patcher.start()
        settings = ServiceSettings(
            type=ZabbixConfig.service_name,
            backend_url='http://example.com',
            username='admin',
            password='admin'
        )
        self.backend = settings.get_backend()

    def tearDown(self):
        self.patcher.stop()

    def test_if_host_exists_its_backend_id_is_updated(self):
        host = factories.HostFactory()

        self.mocked_api().host.get.return_value = [{'hostid': 100}]
        self.backend.create_host(host)
        self.mocked_api().host.get.assert_called_once_with(filter={'host': host.name}, output='hostid')

        host.refresh_from_db()
        self.assertEqual(host.backend_id, str(100))

    def test_if_host_does_not_exist_it_is_created(self):
        host = factories.HostFactory()

        self.mocked_api().host.get.return_value = []
        self.mocked_api().host.create.return_value = {'hostids': [200]}
        self.backend.create_host(host)
        self.assertTrue(self.mocked_api().host.create.called)

        host.refresh_from_db()
        self.assertEqual(host.backend_id, str(200))

    def test_request_exception_is_wrapped(self):
        host = factories.HostFactory()
        self.mocked_api().host.get.side_effect = RequestException()
        self.assertRaises(ServiceBackendError, self.backend.create_host, host)

        self.mocked_api().host.get.side_effect = pyzabbix.ZabbixAPIException()
        self.assertRaises(ServiceBackendError, self.backend.create_host, host)
