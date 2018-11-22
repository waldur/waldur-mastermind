from django.core.exceptions import ObjectDoesNotExist
from rest_framework import test

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

from .. import PLUGIN_NAME, utils


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

    def test_creating_missing_offerings_for_tenants(self):
        tenant = openstack_factories.TenantFactory()
        category = marketplace_factories.CategoryFactory()
        utils.create_missing_offerings(category)
        self.assertEqual(marketplace_models.Offering.objects.all().count(), 1)
        service_settings = self._get_service_settings(tenant)
        self.assertTrue(marketplace_models.Offering.objects.filter(scope=service_settings).exists())

    def test_creating_missing_offerings_for_selected_tenants(self):
        tenant_1 = openstack_factories.TenantFactory()
        tenant_2 = openstack_factories.TenantFactory()
        category = marketplace_factories.CategoryFactory()
        utils.create_missing_offerings(category, [tenant_1.uuid])
        self.assertEqual(marketplace_models.Offering.objects.all().count(), 1)
        self.assertTrue(marketplace_models.Offering.objects.filter(scope=self._get_service_settings(tenant_1)).exists())
        self.assertFalse(marketplace_models.Offering.objects.filter(scope=self._get_service_settings(tenant_2)).exists())

    def _get_service_settings(self, tenant):
        return structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
        )

    def trigger_offering_creation(self):
        fixture = package_fixtures.OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )
        resource = marketplace_factories.ResourceFactory(scope=tenant)
        marketplace_factories.OrderItemFactory(resource=resource)

        tenant.set_ok()
        tenant.save()
        return tenant
