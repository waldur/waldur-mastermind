from waldur_core.core.models import StateMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE, VOLUME_TYPE
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack_tenant.tests import factories as openstack_tenant_factories
from waldur_openstack.openstack_tenant.tests import fixtures as openstack_tenant_fixtures

from .. import utils
from .utils import BaseOpenStackTest


class TemplateImportTest(BaseOpenStackTest):

    def setUp(self):
        super(TemplateImportTest, self).setUp()
        self.fixture = package_fixtures.PackageFixture()
        self.template = self.fixture.openstack_template

    def import_offering(self):
        utils.import_openstack_service_settings(self.fixture.customer)

    def test_plan_is_created(self):
        self.import_offering()
        plan = marketplace_models.Plan.objects.get(scope=self.template)

        self.assertEqual(plan.offering.category, self.tenant_category)
        self.assertEqual(plan.offering.scope, self.template.service_settings)

    def test_duplicate_package_template_is_not_created(self):
        self.import_offering()
        self.assertEqual(1, package_models.PackageTemplate.objects.count())

    def test_price_is_preserved(self):
        self.import_offering()
        plan = marketplace_models.Plan.objects.get(scope=self.template)

        self.assertEqual(self.template.price, plan.unit_price)

    def test_components_are_imported(self):
        self.import_offering()
        plan = marketplace_models.Plan.objects.get(scope=self.template)

        template_components = self.template.components.all()
        plan_components = plan.components.all()
        offering_components = plan.offering.components.all()

        self.assertEqual(plan_components.count(), template_components.count())
        self.assertEqual(offering_components.count(), template_components.count())

    def test_existing_template_is_skipped(self):
        self.import_offering()
        self.import_offering()

        self.assertEqual(marketplace_models.Offering.objects.filter(
            scope=self.template.service_settings).count(), 1)

    def test_shared_settings_flag_is_mapped(self):
        service_settings = self.fixture.openstack_service_settings
        service_settings.shared = True
        service_settings.save()

        self.import_offering()

        offering = marketplace_models.Offering.objects.get(scope=service_settings)
        self.assertTrue(offering.shared)

    def test_setting_without_template_is_imported_without_plans(self):
        self.template.delete()
        self.import_offering()

        service_settings = self.fixture.openstack_service_settings
        offering = marketplace_models.Offering.objects.get(scope=service_settings)

        self.assertEqual(offering.plans.all().count(), 0)

    def test_setting_without_template_is_imported_in_draft_state(self):
        self.template.delete()
        self.import_offering()

        service_settings = self.fixture.openstack_service_settings
        offering = marketplace_models.Offering.objects.get(scope=service_settings)

        self.assertEqual(offering.state, marketplace_models.Offering.States.DRAFT)


class TenantImportTest(BaseOpenStackTest):
    def setUp(self):
        super(TenantImportTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.tenant = self.fixture.tenant

    def import_resource(self):
        utils.import_openstack_service_settings(self.fixture.customer)
        utils.import_openstack_tenants()
        return marketplace_models.Resource.objects.get(scope=self.tenant)

    def test_tenant_attributes_are_imported(self):
        resource = self.import_resource()

        self.assertEqual(resource.attributes['name'], self.tenant.name)
        self.assertEqual(resource.project, self.tenant.project)

    def test_tenant_state_is_imported(self):
        self.tenant.state = StateMixin.States.UPDATING
        self.tenant.save()

        resource = self.import_resource()
        self.assertEqual(resource.state, Resource.States.UPDATING)

    def test_tenant_without_package_does_not_have_plan(self):
        resource = self.import_resource()
        self.assertEqual(resource.plan, None)

    def test_tenant_with_package_has_plan(self):
        self.fixture = package_fixtures.PackageFixture()
        self.tenant = self.fixture.openstack_package.tenant
        self.template = self.fixture.openstack_package.template

        resource = self.import_resource()
        self.assertEqual(resource.plan.scope, self.template)

    def test_existing_resources_are_skipped(self):
        self.import_resource()
        self.import_resource()

        self.assertEqual(marketplace_models.Resource.objects.filter(scope=self.tenant).count(), 1)


class TenantSettingImportTest(BaseOpenStackTest):
    def setUp(self):
        super(TenantSettingImportTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.service_settings = self.fixture.openstack_tenant_service_settings

    def test_instance_offering_has_valid_category(self):
        utils.import_openstack_tenant_service_settings()

        offerings = marketplace_models.Offering.objects.filter(scope=self.service_settings)
        instance_offering = offerings.get(type=INSTANCE_TYPE)
        self.assertTrue(instance_offering.category, self.instance_category)

    def test_volume_offering_has_valid_category(self):
        utils.import_openstack_tenant_service_settings()

        offerings = marketplace_models.Offering.objects.filter(scope=self.service_settings)
        volume_offering = offerings.get(type=VOLUME_TYPE)
        self.assertTrue(volume_offering.category, self.volume_category)

    def test_plan_is_created_for_template(self):
        fixture = package_fixtures.PackageFixture()
        template = fixture.openstack_package.template

        utils.import_openstack_service_settings(fixture.customer)
        utils.import_openstack_tenant_service_settings()

        offerings = marketplace_models.Offering.objects.filter(scope=fixture.openstack_package.service_settings)
        volume_offering = offerings.get(type=VOLUME_TYPE)

        plan = marketplace_models.Plan.objects.get(scope=template, offering=volume_offering)
        self.assertEqual(plan.components.all().count(), template.components.count())


class InstanceImportTest(BaseOpenStackTest):
    def setUp(self):
        super(InstanceImportTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance

    def import_resource(self):
        utils.import_openstack_tenant_service_settings()
        utils.import_openstack_instances_and_volumes()
        return marketplace_models.Resource.objects.get(scope=self.instance)

    def test_attributes_are_imported(self):
        resource = self.import_resource()

        self.assertEqual(resource.attributes['name'], self.instance.name)
        self.assertEqual(resource.project, self.instance.project)

    def test_state_is_imported(self):
        self.instance.state = StateMixin.States.UPDATING
        self.instance.save()

        resource = self.import_resource()
        self.assertEqual(resource.state, Resource.States.UPDATING)

    def test_plan_is_imported(self):
        fixture = package_fixtures.PackageFixture()
        package = fixture.openstack_package
        template = fixture.openstack_template
        service_settings = package.service_settings

        service = openstack_tenant_factories.OpenStackTenantServiceFactory(settings=service_settings)
        spl = openstack_tenant_factories.OpenStackTenantServiceProjectLinkFactory(service=service)
        self.instance = openstack_tenant_factories.InstanceFactory(service_project_link=spl)

        utils.import_openstack_service_settings(fixture.customer)
        resource = self.import_resource()
        self.assertEqual(resource.plan.scope, template)


class VolumeImportTest(BaseOpenStackTest):

    def setUp(self):
        super(VolumeImportTest, self).setUp()
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.volume = self.fixture.volume

    def import_resource(self):
        utils.import_openstack_tenant_service_settings()
        utils.import_openstack_instances_and_volumes()
        return marketplace_models.Resource.objects.get(scope=self.volume)

    def test_attributes_are_imported(self):
        resource = self.import_resource()

        self.assertEqual(resource.attributes['name'], self.volume.name)
        self.assertEqual(resource.project, self.volume.project)

    def test_state_is_imported(self):
        self.volume.state = StateMixin.States.UPDATING
        self.volume.save()

        resource = self.import_resource()
        self.assertEqual(resource.state, Resource.States.UPDATING)

    def test_plan_is_imported(self):
        fixture = package_fixtures.PackageFixture()
        package = fixture.openstack_package
        template = fixture.openstack_template
        service_settings = package.service_settings

        service = openstack_tenant_factories.OpenStackTenantServiceFactory(settings=service_settings)
        spl = openstack_tenant_factories.OpenStackTenantServiceProjectLinkFactory(service=service)
        self.volume = openstack_tenant_factories.VolumeFactory(service_project_link=spl)

        utils.import_openstack_service_settings(fixture.customer)
        resource = self.import_resource()
        self.assertEqual(resource.plan.scope, template)
