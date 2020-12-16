from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)

from .. import INSTANCE_TYPE, VOLUME_TYPE
from .test_order_item import process_order


class BaseOpenstackBackendTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.openstack_tenant_fixture = (
            openstack_tenant_fixtures.OpenStackTenantFixture()
        )
        self.service_settings = (
            self.openstack_tenant_fixture.openstack_tenant_service_settings
        )
        self.subnet = self.openstack_tenant_fixture.subnet

    def trigger_volume_creation(self, **kwargs):
        image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )

        attributes = {
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'Volume',
            'size': 10 * 1024,
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=VOLUME_TYPE, scope=self.service_settings
        )

        order_item = marketplace_factories.OrderItemFactory(
            offering=offering, attributes=attributes
        )
        order_item.order.approve()
        order_item.order.save()

        service = openstack_tenant_models.OpenStackTenantService.objects.create(
            customer=order_item.order.project.customer, settings=self.service_settings,
        )

        openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.create(
            project=order_item.order.project, service=service,
        )

        process_order(order_item.order, self.openstack_tenant_fixture.staff)

        order_item.refresh_from_db()
        return order_item

    def trigger_instance_creation(self, delete_ips=False, **kwargs):
        image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )
        flavor = openstack_tenant_factories.FlavorFactory(
            settings=self.service_settings
        )

        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        attributes = {
            'flavor': openstack_tenant_factories.FlavorFactory.get_url(flavor),
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'virtual-machine',
            'system_volume_size': image.min_disk,
            'internal_ips_set': [{'subnet': subnet_url}],
            'ssh_public_key': structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(
                    user=self.openstack_tenant_fixture.manager
                )
            ),
        }
        attributes.update(kwargs)

        if delete_ips:
            del attributes['internal_ips_set']

        offering = marketplace_factories.OfferingFactory(
            type=INSTANCE_TYPE, scope=self.service_settings
        )
        marketplace_factories.OfferingFactory(
            type=VOLUME_TYPE, scope=self.service_settings
        )
        # Ensure that SPL exists
        self.openstack_tenant_fixture.spl
        order = marketplace_factories.OrderFactory(
            project=self.openstack_tenant_fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            offering=offering, attributes=attributes, order=order,
        )

        process_order(order_item.order, self.openstack_tenant_fixture.owner)

        order_item.refresh_from_db()
        return order_item
