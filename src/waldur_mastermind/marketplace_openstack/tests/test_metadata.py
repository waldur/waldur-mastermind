from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)


class VolumeMetadataTest(BaseOpenStackTest):
    def setUp(self):
        super(VolumeMetadataTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()

        self.volume = self.fixture.volume
        self.resource = marketplace_factories.ResourceFactory(scope=self.volume)

    def test_action_is_synchronized(self):
        self.volume.action = 'detach'
        self.volume.action_details = {'message': 'Detaching volume from instance.'}
        self.volume.save()

        self.resource.refresh_from_db()

        self.assertEqual(self.resource.backend_metadata['action'], self.volume.action)
        self.assertEqual(
            self.resource.backend_metadata['action_details'], self.volume.action_details
        )

    def test_size_is_synchronized(self):
        self.volume.size = 100
        self.volume.save()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_metadata['size'], self.volume.size)

    def test_name_is_synchronized(self):
        self.volume.name = 'new volume name'
        self.volume.save()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.name, self.volume.name)

    def test_state_is_synchronized(self):
        self.volume.set_erred()
        self.volume.save()
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.backend_metadata['state'], self.volume.get_state_display()
        )
        self.assertEqual(
            self.resource.backend_metadata['runtime_state'], self.volume.runtime_state
        )

    def test_instance_is_synchronized(self):
        instance = self.fixture.instance

        self.volume.instance = instance
        self.volume.save()

        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.backend_metadata['instance_uuid'], instance.uuid.hex
        )
        self.assertEqual(self.resource.backend_metadata['instance_name'], instance.name)

    def test_instance_name_is_updated(self):
        instance = self.fixture.instance

        self.volume.instance = instance
        self.volume.save()

        instance.name = 'Name has been changed'
        instance.save()

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_metadata['instance_name'], instance.name)

    def test_instance_has_been_detached(self):
        # Arrange
        instance = self.fixture.instance

        self.volume.instance = instance
        self.volume.save()

        # Act
        self.volume.instance = None
        self.volume.save()

        # Assert
        self.resource.refresh_from_db()
        self.assertIsNone(self.resource.backend_metadata['instance_name'])
        self.assertIsNone(self.resource.backend_metadata['instance_uuid'])


class NetworkMetadataTest(BaseOpenStackTest):
    def setUp(self):
        super(NetworkMetadataTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.resource = marketplace_factories.ResourceFactory(scope=self.instance)

    def test_internal_ip_address_is_synchronized(self):
        internal_ip = self.fixture.internal_ip

        self.resource.refresh_from_db()

        self.assertEqual(
            self.resource.backend_metadata['internal_ips'], internal_ip.fixed_ips
        )

    def test_internal_ip_address_is_updated(self):
        internal_ip = self.fixture.internal_ip
        internal_ip.fixed_ips = [
            {'ip_address': '10.0.0.1', 'subnet_id': internal_ip.subnet.backend_id}
        ]

        internal_ip.save()

        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.backend_metadata['internal_ips'],
            ['10.0.0.1'],
        )

    def test_internal_ip_address_is_updated_on_delete(self):
        internal_ip = self.fixture.internal_ip
        internal_ip.fixed_ips = [
            {'ip_address': '10.0.0.1', 'subnet_id': internal_ip.subnet.backend_id}
        ]
        internal_ip.save()
        self.resource.refresh_from_db()

        internal_ip.delete()

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_metadata['internal_ips'], [])

    def test_floating_ip_address_is_synchronized(self):
        internal_ip = self.fixture.internal_ip
        floating_ip = self.fixture.floating_ip

        floating_ip.internal_ip = internal_ip
        floating_ip.save()

        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.backend_metadata['external_ips'], [floating_ip.address]
        )

    def test_floating_ip_address_is_synchronized_on_delete(self):
        internal_ip = self.fixture.internal_ip
        floating_ip = self.fixture.floating_ip

        floating_ip.internal_ip = internal_ip
        floating_ip.save()

        floating_ip.delete()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_metadata['external_ips'], [])
