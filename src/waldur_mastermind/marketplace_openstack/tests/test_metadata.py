from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import utils
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)


class VolumeMetadataTest(BaseOpenStackTest):
    def setUp(self):
        super(VolumeMetadataTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()

        self.volume = self.fixture.volume
        self.instance = self.fixture.instance

        self.volume.instance = self.instance
        self.volume.save()

    def import_resource(self):
        utils.import_openstack_tenant_service_settings()
        utils.import_openstack_instances_and_volumes()
        return marketplace_models.Resource.objects.get(scope=self.volume)

    def test_volume_metadata_is_imported_when_resource_is_created(self):
        resource = marketplace_factories.ResourceFactory(scope=self.volume)
        resource.refresh_from_db()
        self.assertEqual(resource.backend_metadata['size'], self.volume.size)

    def test_volume_metadata(self):
        self.volume.action = 'detach'
        self.volume.action_details = {'message': 'Detaching volume from instance.'}
        self.volume.save()
        resource = self.import_resource()
        self.assertEqual(resource.name, self.volume.name)
        self.assertEqual(resource.backend_metadata['size'], self.volume.size)
        self.assertEqual(
            resource.backend_metadata['state'], self.volume.get_state_display()
        )
        self.assertEqual(
            resource.backend_metadata['runtime_state'], self.volume.runtime_state
        )
        self.assertEqual(resource.backend_metadata['action'], self.volume.action)
        self.assertEqual(
            resource.backend_metadata['action_details'], self.volume.action_details
        )

    def test_instance(self):
        resource = self.import_resource()
        self.assertEqual(
            resource.backend_metadata['instance_uuid'], self.instance.uuid.hex
        )
        self.assertEqual(resource.backend_metadata['instance_name'], self.instance.name)

        instance = marketplace_models.Resource.objects.get(scope=self.instance)
        self.assertEqual(instance.name, self.instance.name)

    def test_instance_name_is_updated(self):
        resource = self.import_resource()
        self.instance.name = 'Name has been changed'
        self.instance.save()

        resource.refresh_from_db()
        self.assertEqual(resource.backend_metadata['instance_name'], self.instance.name)

        instance = marketplace_models.Resource.objects.get(scope=self.instance)
        self.assertEqual(instance.name, self.instance.name)

    def test_instance_has_been_detached(self):
        resource = self.import_resource()
        self.volume.instance = None
        self.volume.save()

        resource.refresh_from_db()
        self.assertIsNone(resource.backend_metadata['instance_name'])
        self.assertIsNone(resource.backend_metadata['instance_uuid'])


class NetworkMetadataTest(BaseOpenStackTest):
    def setUp(self):
        super(NetworkMetadataTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance

    def import_resource(self):
        utils.import_openstack_tenant_service_settings()
        utils.import_openstack_instances_and_volumes()
        return marketplace_models.Resource.objects.get(scope=self.instance)

    def test_internal_ip_address_is_synchronized(self):
        internal_ip = self.fixture.internal_ip
        resource = self.import_resource()
        self.assertEqual(
            resource.backend_metadata['internal_ips'], internal_ip.fixed_ips
        )

    def test_internal_ip_address_is_updated(self):
        internal_ip = self.fixture.internal_ip
        resource = self.import_resource()
        internal_ip.fixed_ips = [
            {'ip_address': '10.0.0.1', 'subnet_id': internal_ip.subnet.backend_id}
        ]
        internal_ip.save()
        resource.refresh_from_db()
        self.assertEqual(
            resource.backend_metadata['internal_ips'], ['10.0.0.1'],
        )

    def test_internal_ip_address_is_updated_on_delete(self):
        internal_ip = self.fixture.internal_ip
        resource = self.import_resource()
        internal_ip.fixed_ips = [
            {'ip_address': '10.0.0.1', 'subnet_id': internal_ip.subnet.backend_id}
        ]
        internal_ip.save()
        resource.refresh_from_db()

        internal_ip.delete()
        resource.refresh_from_db()
        self.assertEqual(resource.backend_metadata['internal_ips'], [])

    def test_floating_ip_address_is_synchronized(self):
        internal_ip = self.fixture.internal_ip
        floating_ip = self.fixture.floating_ip

        floating_ip.internal_ip = internal_ip
        floating_ip.save()

        resource = self.import_resource()
        self.assertEqual(
            resource.backend_metadata['external_ips'], [floating_ip.address]
        )

    def test_floating_ip_address_is_synchronized_on_delete(self):
        internal_ip = self.fixture.internal_ip
        floating_ip = self.fixture.floating_ip

        floating_ip.internal_ip = internal_ip
        floating_ip.save()

        resource = self.import_resource()

        floating_ip.delete()
        resource.refresh_from_db()
        self.assertEqual(resource.backend_metadata['external_ips'], [])
