from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures

from .. import INSTANCE_TYPE, VOLUME_TYPE


class InstanceCreateLogTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.service_settings = self.tenant.service_settings

    def trigger_instance_creation(self, **kwargs):
        image = openstack_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )
        flavor = openstack_factories.FlavorFactory(settings=self.service_settings)

        subnet_url = openstack_factories.SubNetFactory.get_url(self.fixture.subnet)
        attributes = {
            "flavor": openstack_factories.FlavorFactory.get_url(flavor),
            "image": openstack_factories.ImageFactory.get_url(image),
            "name": "virtual-machine",
            "system_volume_size": image.min_disk,
            "ports": [{"subnet": subnet_url}],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
            ),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=INSTANCE_TYPE, scope=self.tenant
        )
        marketplace_factories.OfferingFactory(type=VOLUME_TYPE, scope=self.tenant)
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )

        marketplace_utils.process_order(order, self.fixture.owner)
