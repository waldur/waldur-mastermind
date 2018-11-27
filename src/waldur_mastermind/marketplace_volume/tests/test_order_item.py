from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.tests import factories as openstack_tenant_factories
from waldur_openstack.openstack_tenant.tests import fixtures as openstack_tenant_fixtures

from .. import PLUGIN_NAME


class OpenStackVolumeOrderItemTest(test.APITransactionTestCase):
    def test_openstack_volume_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_volume_creation()
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.EXECUTING)
        self.assertTrue(openstack_tenant_models.Volume.objects.filter(name='Volume').exists())

    def test_volume_creation_request_payload_is_validated(self):
        order_item = self.trigger_volume_creation(size=100)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)

    def test_volume_state_is_synchronized(self):
        order_item = self.trigger_volume_creation()
        instance = order_item.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

    def trigger_volume_creation(self, **kwargs):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        service_settings = fixture.openstack_tenant_service_settings

        image = openstack_tenant_factories.ImageFactory(
            settings=service_settings,
            min_disk=10240,
            min_ram=1024
        )

        attributes = {
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'Volume',
            'size': 10 * 1024
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, scope=service_settings)

        order_item = marketplace_factories.OrderItemFactory(offering=offering, attributes=attributes)
        order_item.order.approve()
        order_item.order.save()

        service = openstack_tenant_models.OpenStackTenantService.objects.create(
            customer=order_item.order.project.customer,
            settings=service_settings,
        )

        openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.create(
            project=order_item.order.project,
            service=service,
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        return order_item
