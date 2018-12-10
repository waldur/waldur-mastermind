from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack.tests import fixtures as openstack_fixtures
from waldur_openstack.openstack_tenant.tests import fixtures as openstack_tenant_fixtures

from .. import utils
from .utils import BaseOpenStackTest


class OfferingImportTest(BaseOpenStackTest):

    def test_import_offering_for_package(self):
        fixture = package_fixtures.PackageFixture()
        package = fixture.openstack_package

        utils.import_openstack_service_settings(fixture.customer)
        plan = marketplace_models.Plan.objects.get(scope=package.template)

        self.assertEqual(plan.offering.category, self.tenant_category)
        self.assertEqual(plan.offering.scope, package.service_settings.scope.service_settings)
        self.assertEqual(plan.components.all().count(), package.template.components.all().count())

    def test_existing_package_is_skipped(self):
        fixture = package_fixtures.PackageFixture()
        template = fixture.openstack_template

        utils.import_openstack_service_settings(fixture.customer)
        utils.import_openstack_service_settings(fixture.customer)

        self.assertEqual(marketplace_models.Offering.objects.filter(
            scope=template.service_settings).count(), 1)

    def test_import_offering_for_archived_package(self):
        fixture = package_fixtures.PackageFixture()
        package = fixture.openstack_package
        template = package.template
        template.archived = True
        template.save()

        utils.import_openstack_service_settings(fixture.customer)
        offering = marketplace_models.Offering.objects.get(scope=fixture.openstack_service_settings)

        self.assertEqual(offering.state, marketplace_models.Offering.States.ARCHIVED)

    def test_import_offering_for_settings_without_templates(self):
        fixture = openstack_fixtures.OpenStackFixture()
        service_settings = fixture.openstack_service_settings

        utils.import_openstack_service_settings(fixture.customer)

        offering = marketplace_models.Offering.objects.get(scope=service_settings)
        self.assertEqual(offering.plans.all().count(), 0)

    def test_import_offering_for_shared_settings(self):
        fixture = openstack_fixtures.OpenStackFixture()
        service_settings = fixture.openstack_service_settings
        service_settings.shared = True
        service_settings.save()

        utils.import_openstack_service_settings(fixture.customer)

        offering = marketplace_models.Offering.objects.get(scope=service_settings)
        self.assertTrue(offering.shared)

    def test_import_resource_for_tenant(self):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        tenant = fixture.tenant
        utils.import_openstack_service_settings(fixture.customer)
        utils.import_openstack_tenants()

        self.assertTrue(marketplace_models.Resource.objects.filter(scope=tenant).exists())

    def test_import_offerings_for_tenants(self):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        service_settings = fixture.openstack_tenant_service_settings
        utils.import_openstack_tenant_service_settings()

        offerings = marketplace_models.Offering.objects.filter(scope=service_settings)
        self.assertEqual(offerings.count(), 2)

    def test_import_resource_for_instance(self):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        instance = fixture.instance
        utils.import_openstack_tenant_service_settings()
        utils.import_openstack_instances_and_volumes()
        self.assertTrue(marketplace_models.Resource.objects.filter(scope=instance).exists())

    def test_import_resource_for_volume(self):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        volume = fixture.volume
        utils.import_openstack_tenant_service_settings()
        utils.import_openstack_instances_and_volumes()
        self.assertTrue(marketplace_models.Resource.objects.filter(scope=volume).exists())
