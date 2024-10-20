from rest_framework import test

from waldur_core.structure.tests.factories import ProjectFactory, SshPublicKeyFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME
from waldur_openstack.tests import (
    factories as openstack_factories,
)
from waldur_openstack.tests import (
    fixtures as openstack_fixtures,
)
from waldur_rancher import models as rancher_models
from waldur_rancher.tests import factories as rancher_factories


class OrderProcessedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()

    def test_resource_is_created_when_order_is_processed(self):
        service_settings = rancher_factories.RancherServiceSettingsFactory()
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )

        flavor = openstack_factories.FlavorFactory(
            settings=self.fixture.tenant.service_settings,
            ram=1024 * 8,
            cores=8,
        )
        flavor.tenants.add(self.fixture.tenant)
        image = self.fixture.image
        openstack_factories.SecurityGroupFactory(
            name="default", tenant=self.fixture.tenant
        )
        service_settings.options["base_image_name"] = image.name
        service_settings.save()

        ssh_public_key = SshPublicKeyFactory(user=self.fixture.staff)
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=offering,
            attributes={
                "name": "name",
                "tenant": openstack_factories.TenantFactory.get_url(
                    self.fixture.tenant
                ),
                "project": ProjectFactory.get_url(self.fixture.project),
                "ssh_public_key": SshPublicKeyFactory.get_url(ssh_public_key),
                "nodes": [
                    {
                        "subnet": openstack_factories.SubNetFactory.get_url(
                            self.fixture.subnet
                        ),
                        "system_volume_size": 1024,
                        "memory": 1,
                        "cpu": 1,
                        "roles": ["controlplane", "etcd", "worker"],
                    }
                ],
            },
            state=marketplace_models.Order.States.EXECUTING,
        )
        marketplace_utils.process_order(order, self.fixture.staff)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(name="name").exists()
        )
        self.assertTrue(rancher_models.Cluster.objects.filter(name="name").exists())
