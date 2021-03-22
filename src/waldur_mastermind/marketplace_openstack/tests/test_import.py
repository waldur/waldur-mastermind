from unittest import mock
from unittest.mock import patch

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure import signals as structure_signals
from waldur_core.structure.models import ServiceSettings
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.factories import OfferingFactory
from waldur_mastermind.marketplace_openstack import (
    INSTANCE_TYPE,
    TENANT_TYPE,
    VOLUME_TYPE,
)
from waldur_mastermind.marketplace_openstack.tests.mocks import (
    MOCK_FLAVOR,
    MOCK_INSTANCE,
    MOCK_TENANT,
    MOCK_VOLUME,
)
from waldur_openstack.openstack import models
from waldur_openstack.openstack.tests.factories import TenantFactory
from waldur_openstack.openstack.tests.fixtures import OpenStackFixture
from waldur_openstack.openstack.tests.test_tenant import BaseTenantActionsTest
from waldur_openstack.openstack.tests.unittests.test_backend import BaseBackendTestCase
from waldur_openstack.openstack_tenant.tests.factories import (
    InstanceFactory,
    VolumeFactory,
)
from waldur_openstack.openstack_tenant.tests.fixtures import OpenStackTenantFixture

from .mocks import MockTenant
from .utils import BaseOpenStackTest


class ImportAsMarketplaceResourceTest(BaseOpenStackTest):
    def setUp(self):
        super(ImportAsMarketplaceResourceTest, self).setUp()
        self.fixture = OpenStackTenantFixture()

    def test_import_volume_as_marketplace_resource(self):
        volume = self.fixture.volume
        marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_tenant_service_settings, type=VOLUME_TYPE
        )

        structure_signals.resource_imported.send(
            sender=volume.__class__, instance=volume,
        )

        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=volume).exists()
        )

    def test_import_instance_as_marketplace_resource(self):
        instance = self.fixture.instance
        marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_tenant_service_settings, type=INSTANCE_TYPE
        )

        structure_signals.resource_imported.send(
            sender=instance.__class__, instance=instance,
        )

        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=instance).exists()
        )

    def test_import_tenant_as_marketplace_resource(self):
        tenant = self.fixture.tenant
        self.import_tenant(tenant)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=tenant).exists()
        )

    def test_when_tenant_is_imported_volume_and_instance_offerings_are_created(self):
        tenant = self.fixture.tenant
        self.import_tenant(tenant)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(type=INSTANCE_TYPE).exists()
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(type=VOLUME_TYPE).exists()
        )

    def import_tenant(self, tenant):
        marketplace_factories.OfferingFactory(
            scope=tenant.service_settings, type=TENANT_TYPE
        )

        structure_signals.resource_imported.send(
            sender=tenant.__class__, instance=tenant,
        )


class BaseInstanceImportTest(BaseBackendTestCase, BaseOpenStackTest):
    def setUp(self):
        super(BaseInstanceImportTest, self).setUp()
        self.fixture = OpenStackTenantFixture()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_tenant_service_settings,
            type=INSTANCE_TYPE,
            shared=False,
            customer=self.fixture.customer,
        )
        self.mocked_nova().servers.list.return_value = [MOCK_INSTANCE]
        self.mocked_nova().servers.get.return_value = MOCK_INSTANCE
        self.mocked_nova().flavors.get.return_value = MOCK_FLAVOR
        self.mocked_nova().volumes.get_server_volumes.return_value = []


class InstanceImportableResourcesTest(BaseInstanceImportTest):
    def setUp(self):
        super(InstanceImportableResourcesTest, self).setUp()
        self.url = OfferingFactory.get_url(self.offering, 'importable_resources')
        self.client.force_authenticate(self.fixture.owner)

    def test_importable_instances_are_returned(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(
            response.data,
            [
                {
                    'type': 'OpenStackTenant.Instance',
                    'name': 'VM-1',
                    'backend_id': '1',
                    'description': '',
                    'extra': [
                        {'name': 'Runtime state', 'value': 'active'},
                        {'name': 'Flavor', 'value': 'Standard'},
                        {'name': 'RAM (MBs)', 'value': 4096},
                        {'name': 'Cores', 'value': 4},
                    ],
                }
            ],
        )
        self.mocked_nova().servers.list.assert_called()
        self.mocked_nova().flavors.get.assert_called()


class InstanceImportTest(BaseInstanceImportTest):
    def setUp(self):
        super(InstanceImportTest, self).setUp()
        self.url = OfferingFactory.get_url(self.offering, 'import_resource')
        self.client.force_authenticate(self.fixture.owner)

    def _get_payload(self, backend_id='backend_id'):
        return {
            'backend_id': backend_id,
            'project': self.fixture.project.uuid.hex,
        }

    @mock.patch(
        'waldur_openstack.openstack_tenant.executors.InstancePullExecutor.execute'
    )
    def test_instance_can_be_imported(self, resource_import_execute_mock):
        response = self.client.post(self.url, self._get_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource_import_execute_mock.assert_called()
        instance = marketplace_models.Resource.objects.get()
        self.assertEqual(instance.backend_id, '1')

    def test_existing_instance_cannot_be_imported(self):
        InstanceFactory(
            service_settings=self.fixture.openstack_tenant_service_settings,
            backend_id=MOCK_INSTANCE.id,
        )
        payload = self._get_payload(MOCK_INSTANCE.id)

        response = self.client.post(self.url, payload)

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


class BaseVolumeImportTest(BaseBackendTestCase, test.APITransactionTestCase):
    def setUp(self):
        super(BaseVolumeImportTest, self).setUp()
        self.fixture = OpenStackTenantFixture()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_tenant_service_settings,
            type=VOLUME_TYPE,
            shared=False,
            customer=self.fixture.customer,
        )
        self.mocked_cinder().volumes.list.return_value = [MOCK_VOLUME]
        self.mocked_cinder().volumes.get.return_value = MOCK_VOLUME


class VolumeImportableResourcesTest(BaseVolumeImportTest):
    def setUp(self):
        super(VolumeImportableResourcesTest, self).setUp()
        self.url = OfferingFactory.get_url(self.offering, 'importable_resources')
        self.client.force_authenticate(self.fixture.owner)

    def test_importable_volumes_are_returned(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            [
                {
                    'type': 'OpenStackTenant.Volume',
                    'name': 'ssd-volume',
                    'backend_id': None,
                    'description': '',
                    'extra': [
                        {'name': 'Is bootable', 'value': False},
                        {'name': 'Size', 'value': 102400},
                        {'name': 'Device', 'value': ''},
                        {'name': 'Runtime state', 'value': 'available'},
                    ],
                }
            ],
        )


class VolumeImportTest(BaseVolumeImportTest):
    def setUp(self):
        super(VolumeImportTest, self).setUp()
        self.url = OfferingFactory.get_url(self.offering, 'import_resource')
        self.client.force_authenticate(self.fixture.owner)

    def test_backend_volume_is_imported(self):
        response = self.client.post(self.url, self._get_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = marketplace_models.Resource.objects.get()
        self.assertEqual(instance.backend_id, '1')

    def test_backend_volume_cannot_be_imported_if_it_is_registered_in_waldur(self):
        volume = VolumeFactory(
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )

        response = self.client.post(self.url, self._get_payload(volume.backend_id))

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def _get_payload(self, backend_id='backend_id'):
        return {
            'backend_id': backend_id,
            'project': self.fixture.project.uuid.hex,
        }


@ddt
class TenantImportableResourcesTest(BaseBackendTestCase, BaseTenantActionsTest):
    def setUp(self):
        super(TenantImportableResourcesTest, self).setUp()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_service_settings, type=TENANT_TYPE
        )
        self.url = OfferingFactory.get_url(self.offering, 'importable_resources')

    def test_user_can_list_importable_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        self.mocked_keystone().projects.list.return_value = [
            MockTenant(name='First Tenant', id='1'),
            MockTenant(name='Second Tenant', id='2'),
        ]

        response = self.client.get(self.url)

        self.assertEquals(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEquals(
            response.data,
            [
                {
                    'type': 'OpenStack.Tenant',
                    'name': 'First Tenant',
                    'description': '',
                    'backend_id': '1',
                },
                {
                    'type': 'OpenStack.Tenant',
                    'name': 'Second Tenant',
                    'description': '',
                    'backend_id': '2',
                },
            ],
        )

    @data('admin', 'manager', 'owner')
    def test_user_does_not_have_permissions_to_list_resources(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)

        self.assertEquals(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class TenantImportTest(BaseBackendTestCase):
    def setUp(self):
        super(TenantImportTest, self).setUp()
        self.fixture = OpenStackFixture()
        self.backend_tenant = TenantFactory.build(
            service_settings=self.fixture.openstack_service_settings,
            project=self.fixture.project,
        )
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_service_settings, type=TENANT_TYPE
        )

    def test_tenant_is_imported(self):
        response = self.import_tenant()

        self.assertEquals(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEquals(response.data['backend_id'], self.backend_tenant.backend_id)
        self.assertTrue(
            models.Tenant.objects.filter(
                backend_id=self.backend_tenant.backend_id
            ).exists()
        )

    @patch('waldur_core.structure.handlers.event_logger')
    def test_event_is_emitted(self, logger_mock):
        self.import_tenant()

        actual = logger_mock.resource.info.call_args[0][0]
        expected = 'Resource {resource_full_name} has been imported.'
        self.assertEqual(expected, actual)

    @data('admin', 'manager', 'owner')
    def test_user_cannot_import_tenant(self, user):
        response = self.import_tenant(user)
        self.assertEquals(
            response.status_code, status.HTTP_403_FORBIDDEN, response.data
        )

    def test_tenant_cannot_be_imported_if_backend_id_exists_already(self):
        self.backend_tenant.save()
        response = self.import_tenant()
        self.assertEquals(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_imported_tenant_has_user_password_and_username(self):
        response = self.import_tenant()

        self.assertEquals(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEquals(response.data['backend_id'], self.backend_tenant.backend_id)

        tenant = models.Tenant.objects.get(backend_id=self.backend_tenant.backend_id)
        self.assertIsNotNone(tenant.user_username)
        self.assertIsNotNone(tenant.user_password)

    def test_imported_tenant_settings_have_username_and_password_set(self):
        response = self.import_tenant()
        self.assertEquals(response.status_code, status.HTTP_201_CREATED, response.data)

        tenant = models.Tenant.objects.get(backend_id=self.backend_tenant.backend_id)
        service_settings = ServiceSettings.objects.get(scope=tenant)

        self.assertEquals(tenant.user_username, service_settings.username)
        self.assertEquals(tenant.user_password, service_settings.password)

    def import_tenant(self, user='staff'):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'backend_id': self.backend_tenant.backend_id,
            'project': self.fixture.project.uuid.hex,
        }
        url = OfferingFactory.get_url(self.offering, 'import_resource')
        self.mocked_keystone.return_value.projects.get.return_value = MOCK_TENANT
        return self.client.post(url, payload)
