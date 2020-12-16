from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)
from waldur_openstack.openstack_tenant.tests.helpers import (
    override_openstack_tenant_settings,
)

from .. import VOLUME_TYPE
from .helpers import BaseOpenstackBackendTest
from .test_order_item import process_order


class VolumeNameCreateTest(BaseOpenstackBackendTest):
    def setUp(self):
        super(VolumeNameCreateTest, self).setUp()
        self.image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings
        )
        self.image_url = openstack_tenant_factories.ImageFactory.get_url(self.image)
        self.spl_url = openstack_tenant_factories.OpenStackTenantServiceProjectLinkFactory.get_url(
            self.openstack_tenant_fixture.spl
        )

    def test_image_name_populated_on_volume_creation(self):
        order_item = self.trigger_volume_creation(image=self.image_url)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        self.assertEqual(order_item.resource.scope.image.name, self.image.name)

    def test_volume_image_name_populated_on_instance_creation(self):
        flavor = openstack_tenant_factories.FlavorFactory(
            settings=self.service_settings
        )
        flavor_url = openstack_tenant_factories.FlavorFactory.get_url(flavor)
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)

        payload = {
            'name': 'test-instance',
            'image': self.image_url,
            'service_project_link': self.spl_url,
            'flavor': flavor_url,
            'system_volume_size': 20480,
            'internal_ips_set': [{'subnet': subnet_url}],
        }

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

        system_volume = order_item.resource.scope.volumes.first()
        self.assertEqual(system_volume.image.name, self.image.name)

    def test_create_instance_with_data_volumes_with_different_names(self):
        flavor = openstack_tenant_factories.FlavorFactory(
            settings=self.service_settings
        )
        flavor_url = openstack_tenant_factories.FlavorFactory.get_url(flavor)
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(
            self.openstack_tenant_fixture.subnet
        )

        payload = {
            'name': 'test-instance',
            'image': self.image_url,
            'service_project_link': self.spl_url,
            'flavor': flavor_url,
            'system_volume_size': 20480,
            'internal_ips_set': [{'subnet': subnet_url}],
            'data_volumes': [
                {
                    'size': 1024,
                    'type': openstack_tenant_factories.VolumeTypeFactory.get_url(),
                },
                {
                    'size': 1024 * 3,
                    'type': openstack_tenant_factories.VolumeTypeFactory.get_url(),
                },
            ],
        }

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        volumes = order_item.resource.scope.volumes.all()

        data_volumes_names = [v.name for v in volumes if not v.bootable]
        self.assertEqual(
            set(['test-instance-data-3', 'test-instance-data-2']),
            set(data_volumes_names),
        )


class VolumeTypeCreateTest(BaseOpenstackBackendTest):
    def setUp(self):
        super(VolumeTypeCreateTest, self).setUp()
        self.type = openstack_tenant_factories.VolumeTypeFactory(
            settings=self.service_settings, backend_id='ssd'
        )
        self.type_url = openstack_tenant_factories.VolumeTypeFactory.get_url(self.type)

    def test_type_populated_on_volume_creation(self):
        order_item = self.trigger_volume_creation(type=self.type_url)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        volume_type = order_item.resource.scope.type
        self.assertEqual(
            openstack_tenant_factories.VolumeTypeFactory.get_url(volume_type),
            self.type_url,
        )

    def test_volume_type_should_be_related_to_the_same_service_settings(self):
        order_item = self.trigger_volume_creation(
            type=openstack_tenant_factories.VolumeTypeFactory.get_url()
        )
        self.assertEqual(order_item.state, order_item.States.ERRED)
        self.assertIn('type', order_item.error_message)

    def test_when_volume_is_created_volume_type_quota_is_updated(self):
        self.trigger_volume_creation(type=self.type_url, size=1024 * 10)

        key = 'gigabytes_' + self.type.backend_id
        usage = self.service_settings.quotas.get(name=key).usage
        self.assertEqual(usage, 10)

    def test_user_can_not_create_volume_if_resulting_quota_usage_is_greater_than_limit(
        self,
    ):
        self.service_settings.set_quota_usage('gigabytes_ssd', 0)
        self.service_settings.set_quota_limit('gigabytes_ssd', 0)

        order_item = self.trigger_volume_creation(type=self.type_url, size=1024)
        self.assertEqual(order_item.state, order_item.States.ERRED)


class VolumeAvailabilityZoneCreateTest(BaseOpenstackBackendTest):
    def test_availability_zone_should_be_related_to_the_same_service_settings(self):
        order_item = self.trigger_volume_creation(
            availability_zone=openstack_tenant_factories.VolumeAvailabilityZoneFactory.get_url()
        )
        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_availability_zone_should_be_available(self):
        zone = self.openstack_tenant_fixture.volume_availability_zone
        zone.available = False
        zone.save()

        order_item = self.trigger_volume_creation(
            availability_zone=openstack_tenant_factories.VolumeAvailabilityZoneFactory.get_url(
                zone
            )
        )
        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_availability_zone_name_is_validated(self):
        zone = self.openstack_tenant_fixture.volume_availability_zone

        order_item = self.trigger_volume_creation(
            availability_zone=openstack_tenant_factories.VolumeAvailabilityZoneFactory.get_url(
                zone
            )
        )
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

    @override_openstack_tenant_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_exists_validation_fails(self):
        self.openstack_tenant_fixture.volume_availability_zone
        order_item = self.trigger_volume_creation()
        self.assertEqual(order_item.state, order_item.States.ERRED)

    @override_openstack_tenant_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_does_not_exist_validation_succeeds(
        self,
    ):
        order_item = self.trigger_volume_creation()
        self.assertEqual(order_item.state, order_item.States.EXECUTING)


class VolumeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.openstack_tenant_fixture = (
            openstack_tenant_fixtures.OpenStackTenantFixture()
        )

        self.volume = self.openstack_tenant_fixture.volume
        self.volume.runtime_state = 'available'
        self.volume.save()

        self.offering = marketplace_factories.OfferingFactory(type=VOLUME_TYPE)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.volume, offering=self.offering
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.openstack_tenant_fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )
        self.spl = self.volume.service_project_link

    def trigger_deletion(self):
        process_order(self.order_item.order, self.openstack_tenant_fixture.staff)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.volume.refresh_from_db()

    def test_spl_quota_updated_by_signal_handler_when_volume_is_removed(self):
        self.volume.delete()
        Quotas = openstack_tenant_models.OpenStackTenantServiceProjectLink.Quotas
        self.assertEqual(self.spl.quotas.get(name=Quotas.storage).usage, 0)

    def test_erred_volume_can_be_destroyed(self):
        self.volume.state = openstack_tenant_models.Volume.States.ERRED
        self.volume.save()
        self.trigger_deletion()
        self.assertEqual(self.order_item.state, self.order_item.States.EXECUTING)

    def test_attached_volume_can_not_be_destroyed(self):
        self.volume.state = openstack_tenant_models.Volume.States.OK
        self.volume.runtime_state = 'in-use'
        self.volume.save()
        self.trigger_deletion()
        self.assertEqual(self.order_item.state, self.order_item.States.ERRED)

    def test_pending_volume_can_not_be_destroyed(self):
        self.volume.state = openstack_tenant_models.Volume.States.CREATING
        self.volume.save()
        self.trigger_deletion()
        self.assertEqual(self.order_item.state, self.order_item.States.ERRED)
