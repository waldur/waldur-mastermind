from waldur_core.structure import signals as structure_signals
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    INSTANCE_TYPE,
    TENANT_TYPE,
    VOLUME_TYPE,
)
from waldur_openstack.openstack_tenant.tests.fixtures import OpenStackTenantFixture

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
