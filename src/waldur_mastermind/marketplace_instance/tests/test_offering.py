from django.core.exceptions import ObjectDoesNotExist
from rest_framework import test

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack import models as openstack_models

from .. import PLUGIN_NAME


class OpenStackInstanceOfferingTest(test.APITransactionTestCase):
    def test_offering_is_created_when_tenant_creation_is_completed(self):
        tenant = self.trigger_offering_creation()

        offering = marketplace_models.Offering.objects.get(type=PLUGIN_NAME)
        service_settings = offering.scope

        self.assertTrue(isinstance(service_settings, structure_models.ServiceSettings))
        self.assertEqual(service_settings.scope, tenant)

    def test_offering_is_not_created_if_tenant_is_not_created_via_marketplace(self):
        fixture = package_fixtures.OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )

        tenant.set_ok()
        tenant.save()

        self.assertRaises(ObjectDoesNotExist, marketplace_models.Offering.objects.get, type=PLUGIN_NAME)

    def test_offering_is_archived_when_tenant_is_deleted(self):
        tenant = self.trigger_offering_creation()
        tenant.delete()
        offering = marketplace_models.Offering.objects.get(type=PLUGIN_NAME)
        self.assertTrue(offering.state, marketplace_models.Offering.States.ARCHIVED)

    def trigger_offering_creation(self):
        fixture = package_fixtures.OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )
        marketplace_factories.OrderItemFactory(scope=tenant)

        tenant.set_ok()
        tenant.save()
        return tenant
