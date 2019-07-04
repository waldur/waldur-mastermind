from decimal import Decimal

from freezegun import freeze_time

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import serializers as packages_serializers
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack import models as openstack_models

from .utils import BaseOpenStackTest
from .. import PACKAGE_TYPE


class InvoiceTest(BaseOpenStackTest):
    @freeze_time('2017-01-01')
    def setUp(self):
        super(InvoiceTest, self).setUp()
        self.fixture = package_fixtures.PackageFixture()
        self.project_url = structure_factories.ProjectFactory.get_url(self.fixture.project)

        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_service_settings,
            type=PACKAGE_TYPE,
            state=marketplace_models.Offering.States.ACTIVE,
        )

    @freeze_time('2017-01-31')
    def test_plan_with_monthly_pricing(self):
        with freeze_time('2017-01-01'):
            self._init()

        with freeze_time('2017-01-10'):
            invoice = self._get_invoice()
            self.assertEqual(invoice.price, 30)

    @freeze_time('2017-01-10')
    def test_change_to_more_expensive_plan(self):
        with freeze_time('2017-01-01'):
            self._init(Decimal(10) / 31, UnitPriceMixin.Units.PER_DAY)

        with freeze_time('2017-01-10'):
            new_plan = self._create_plan(Decimal(20) / 31, UnitPriceMixin.Units.PER_DAY)
            self._switch_plan(new_plan)
            invoice = self._get_invoice()
            control_value = quantize_price(Decimal(60) / 31) * 22 + quantize_price(Decimal(30) / 31) * 9  # 51.41
            self.assertEqual(invoice.price, control_value)

    @freeze_time('2017-01-10')
    def test_change_to_more_cheap_plan(self):
        with freeze_time('2017-01-01'):
            self._init(Decimal(10) / 31, UnitPriceMixin.Units.PER_DAY)

        with freeze_time('2017-01-10'):
            new_plan = self._create_plan(Decimal(1) / 31, UnitPriceMixin.Units.PER_DAY)
            self._switch_plan(new_plan)
            invoice = self._get_invoice()
            control_value = quantize_price(Decimal(3) / 31) * 21 + quantize_price(Decimal(30) / 31) * 10  # 11.80
            self.assertEqual(invoice.price, control_value)

    def test_change_from_daily_to_monthly_plan(self):
        with freeze_time('2017-01-01'):
            self._init(Decimal(10) / 31, UnitPriceMixin.Units.PER_DAY)

        with freeze_time('2017-01-10'):
            new_plan = self._create_plan(10, UnitPriceMixin.Units.PER_MONTH)
            self._switch_plan(new_plan)

            invoice = self._get_invoice()
            control_value = quantize_price(Decimal(21) / 31) * 30 + 9 * 30  # 290.4
            self.assertEqual(invoice.price, control_value)

    def _create_plan(self, component_price=10, unit=None):
        unit = unit or UnitPriceMixin.Units.PER_MONTH
        plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            unit=unit
        )

        for component_type in ('ram', 'cores', 'storage'):
            offering_component, _ = marketplace_models.OfferingComponent.objects.get_or_create(
                offering=self.offering,
                type=component_type
            )
            marketplace_factories.PlanComponentFactory(
                component=offering_component,
                plan=plan,
                price=Decimal(component_price)
            )
        return plan

    def _switch_plan(self, new_plan):
        new_plan_url = marketplace_factories.PlanFactory.get_url(new_plan)

        payload = {
            'plan': new_plan_url,
        }

        user = self.fixture.staff
        self.client.force_login(user)
        url = marketplace_factories.ResourceFactory.get_url(self.resource, 'switch_plan')
        self.client.post(url, payload)
        update_order_item = marketplace_models.OrderItem.objects.get(
            resource=self.resource,
            type=marketplace_models.OrderItem.Types.UPDATE)

        marketplace_utils.process_order_item(update_order_item, user)

        new_template = update_order_item.plan.scope
        service_settings = self.package.service_settings

        packages_serializers._set_tenant_quotas(self.tenant, new_template)
        packages_serializers._set_related_service_settings_quotas(self.tenant, new_template)
        packages_serializers._set_tenant_extra_configuration(self.tenant, new_template)
        self.package.delete()
        package_models.OpenStackPackage.objects.create(
            template=new_template,
            service_settings=service_settings,
            tenant=self.tenant
        )

        callbacks.resource_update_succeeded(self.resource)

    def _init(self, component_price=10, unit=None):
        unit = unit or UnitPriceMixin.Units.PER_MONTH
        self.offering_url = marketplace_factories.OfferingFactory.get_url(self.offering)

        plan = self._create_plan(component_price=component_price, unit=unit)
        plan_url = marketplace_factories.PlanFactory.get_url(plan)

        # Create SPL
        self.fixture.openstack_spl

        attributes = dict(
            name='My first VPC',
            description='Database cluster',
            user_username='admin_user',
        )

        payload = {
            'project': self.project_url,
            'items': [
                {
                    'offering': self.offering_url,
                    'plan': plan_url,
                    'attributes': attributes,
                },
            ]
        }

        user = self.fixture.staff
        self.client.force_login(user)
        url = marketplace_factories.OrderFactory.get_list_url()
        response = self.client.post(url, payload)
        self.order_item = marketplace_models.OrderItem.objects.get(uuid=response.data['items'][0]['uuid'])
        marketplace_utils.process_order_item(self.order_item, user)
        self.resource = self.order_item.resource
        callbacks.resource_creation_succeeded(self.resource)
        self.tenant = plan.scope.openstack_packages.first().tenant
        self.tenant.state = openstack_models.Tenant.States.OK
        self.tenant.backend_id = 'tenant id'
        self.tenant.save()
        self.package = package_models.OpenStackPackage.objects.get(tenant=self.tenant)

    def _get_invoice(self):
        return invoices_models.Invoice.objects.get(customer=self.fixture.customer)
