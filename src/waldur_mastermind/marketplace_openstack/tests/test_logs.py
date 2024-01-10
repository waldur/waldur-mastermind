from unittest import mock

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)

from .. import INSTANCE_TYPE, VOLUME_TYPE


class InstanceCreateLogTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.service_settings = self.fixture.openstack_tenant_service_settings

    @mock.patch("waldur_mastermind.marketplace.handlers.log")
    def test_ranamed_log_is_not_created_if_instance_has_been_created(self, mock_log):
        self.trigger_instance_creation()
        mock_log.log_marketplace_resource_renamed.assert_not_called()

    def trigger_instance_creation(self, **kwargs):
        image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )
        flavor = openstack_tenant_factories.FlavorFactory(
            settings=self.service_settings
        )

        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(
            self.fixture.subnet
        )
        attributes = {
            "flavor": openstack_tenant_factories.FlavorFactory.get_url(flavor),
            "image": openstack_tenant_factories.ImageFactory.get_url(image),
            "name": "virtual-machine",
            "system_volume_size": image.min_disk,
            "internal_ips_set": [{"subnet": subnet_url}],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
            ),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=INSTANCE_TYPE, scope=self.service_settings
        )
        marketplace_factories.OfferingFactory(
            type=VOLUME_TYPE, scope=self.service_settings
        )
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )

        marketplace_utils.process_order(order, self.fixture.owner)
