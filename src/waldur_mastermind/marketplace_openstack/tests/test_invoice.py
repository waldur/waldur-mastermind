from decimal import Decimal
from unittest import mock

from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import signals as marketplace_signals
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
)
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import serializers as packages_serializers
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_mastermind.packages.tests import utils as openstack_test_utils
from waldur_openstack.openstack import models as openstack_models

from .. import PACKAGE_TYPE
from .utils import BaseOpenStackTest, override_plugin_settings


@openstack_test_utils.override_plugin_settings(BILLING_ENABLED=True)
class InvoiceTest(BaseOpenStackTest):
    def test_plan_with_monthly_pricing(self):
        with freeze_time('2017-01-01'):
            self._init(price=10, unit=UnitPriceMixin.Units.PER_MONTH)

        with freeze_time('2017-01-10'):
            self.assertEqual(self.invoice.price, 10)

    def test_switch_to_more_expensive_plan(self):
        with freeze_time('2017-01-01'):
            self._init(price=30, unit=UnitPriceMixin.Units.PER_DAY)

        with freeze_time('2017-01-10'):
            self._switch_plan(60, UnitPriceMixin.Units.PER_DAY)
            self.assertEqual(self.invoice.price, 30 * 9 + 60 * 22)

    def test_switch_to_more_cheap_plan(self):
        with freeze_time('2017-01-01'):
            self._init(60, UnitPriceMixin.Units.PER_DAY)

        with freeze_time('2017-01-10'):
            self._switch_plan(30, UnitPriceMixin.Units.PER_DAY)
            self.assertEqual(self.invoice.price, 60 * 10 + 30 * 21)

    def test_change_from_daily_to_monthly_plan(self):
        """
        Monthly plan is ignored in invoicing because it is cheaper than daily plan.
        """
        with freeze_time('2017-01-01'):
            self._init(60, UnitPriceMixin.Units.PER_DAY)

        with freeze_time('2017-01-10'):
            self._switch_plan(30, UnitPriceMixin.Units.PER_MONTH)
            self.assertEqual(self.invoice.price, 60 * 31)

    def _create_plan(self, price, unit):
        plan = marketplace_factories.PlanFactory(offering=self.offering, unit=unit)

        components = package_models.PackageTemplate.get_required_component_types()
        for component_type in components:
            (
                offering_component,
                _,
            ) = marketplace_models.OfferingComponent.objects.get_or_create(
                offering=self.offering, type=component_type
            )
            marketplace_factories.PlanComponentFactory(
                component=offering_component,
                plan=plan,
                price=Decimal(price) / len(components),
            )
        return plan

    def _switch_plan(self, price, unit):
        new_plan = self._create_plan(price, unit)
        new_plan_url = marketplace_factories.PlanFactory.get_url(new_plan)

        user = self.fixture.staff
        self.client.force_login(user)
        url = marketplace_factories.ResourceFactory.get_url(
            self.resource, 'switch_plan'
        )
        self.client.post(url, {'plan': new_plan_url,})
        update_order_item = marketplace_models.OrderItem.objects.get(
            resource=self.resource, type=marketplace_models.OrderItem.Types.UPDATE
        )

        marketplace_utils.process_order_item(update_order_item, user)

        new_template = update_order_item.plan.scope
        packages_serializers._set_tenant_quotas(self.tenant, new_template)
        packages_serializers._set_related_service_settings_quotas(
            self.tenant, new_template
        )
        packages_serializers._set_tenant_extra_configuration(self.tenant, new_template)
        self.package.delete()
        package_models.OpenStackPackage.objects.create(
            template=new_template,
            service_settings=self.package.service_settings,
            tenant=self.tenant,
        )

        callbacks.resource_update_succeeded(self.resource)

    def _init(self, price, unit):
        self.fixture = package_fixtures.PackageFixture()
        self.project_url = structure_factories.ProjectFactory.get_url(
            self.fixture.project
        )

        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_service_settings,
            type=PACKAGE_TYPE,
            state=marketplace_models.Offering.States.ACTIVE,
        )

        self.offering_url = marketplace_factories.OfferingFactory.get_url(self.offering)

        plan = self._create_plan(price=price, unit=unit)
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
            ],
        }

        user = self.fixture.staff
        self.client.force_login(user)
        url = marketplace_factories.OrderFactory.get_list_url()
        response = self.client.post(url, payload)

        order_item = marketplace_models.OrderItem.objects.get(
            uuid=response.data['items'][0]['uuid']
        )
        marketplace_utils.process_order_item(order_item, user)
        self.resource = order_item.resource
        callbacks.resource_creation_succeeded(self.resource)

        self.tenant = plan.scope.openstack_packages.first().tenant
        self.tenant.state = openstack_models.Tenant.States.OK
        self.tenant.backend_id = 'tenant id'
        self.tenant.save()

        self.package = package_models.OpenStackPackage.objects.get(tenant=self.tenant)
        self.invoice = invoices_models.Invoice.objects.get(
            customer=self.fixture.customer
        )


@override_plugin_settings(BILLING_ENABLED=True)
@freeze_time('2019-09-10')
class MarketplaceInvoiceBaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory(type=PACKAGE_TYPE)
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.limits = {
            RAM_TYPE: 1 * 1024,
            CORES_TYPE: 2,
            STORAGE_TYPE: 3 * 1024,
        }
        self.prices = {
            RAM_TYPE: 10,
            CORES_TYPE: 100,
            STORAGE_TYPE: 1,
        }
        for ct in self.prices.keys():
            marketplace_factories.PlanComponentFactory(
                plan=self.plan,
                component=marketplace_factories.OfferingComponentFactory(
                    offering=self.offering, type=ct,
                ),
                price=self.prices[ct],
            )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            limits=self.limits,
            state=marketplace_models.Resource.States.OK,
        )


class MarketplaceInvoiceTest(MarketplaceInvoiceBaseTest):
    def test_when_resource_is_created_invoice_is_updated(self):
        marketplace_signals.resource_creation_succeeded.send(
            sender=self.resource.__class__, instance=self.resource,
        )
        invoice_item = invoices_models.InvoiceItem.objects.get(scope=self.resource)
        self.assertEqual(
            invoice_item.unit_price,
            self.limits[RAM_TYPE] * self.prices[RAM_TYPE] / 1024
            + self.limits[CORES_TYPE] * self.prices[CORES_TYPE]
            + self.limits[STORAGE_TYPE] * self.prices[STORAGE_TYPE] / 1024,
        )

    def test_when_resource_is_updated_invoice_is_updated(self):
        marketplace_signals.resource_creation_succeeded.send(
            sender=self.resource.__class__, instance=self.resource,
        )
        self.resource.limits = {
            RAM_TYPE: 10 * 1024,
            CORES_TYPE: 20,
            STORAGE_TYPE: 30 * 1024,
        }
        self.resource.save()
        order = marketplace_factories.OrderFactory(
            project=self.resource.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=self.offering,
            resource=self.resource,
            state=marketplace_models.OrderItem.States.EXECUTING,
            limits=self.resource.limits,
        )
        marketplace_signals.limit_update_succeeded.send(
            sender=self.resource.__class__, order_item=order_item,
        )
        invoice_items = invoices_models.InvoiceItem.objects.filter(scope=self.resource)

        self.assertEqual(invoice_items.count(), 2)
        self.assertNotEqual(
            invoice_items.last().unit_price, invoice_items.first().unit_price
        )
        self.assertEqual(
            invoice_items.last().unit_price,
            self.resource.limits[RAM_TYPE] * self.prices[RAM_TYPE] / 1024
            + self.resource.limits[CORES_TYPE] * self.prices[CORES_TYPE]
            + self.resource.limits[STORAGE_TYPE] * self.prices[STORAGE_TYPE] / 1024,
        )

    def test_when_resource_is_deleted_invoice_is_updated(self):
        marketplace_signals.resource_creation_succeeded.send(
            sender=self.resource.__class__, instance=self.resource,
        )
        with freeze_time('2019-09-18'):
            marketplace_signals.resource_deletion_succeeded.send(
                sender=self.resource.__class__, instance=self.resource,
            )
        invoice_item = invoices_models.InvoiceItem.objects.get(scope=self.resource)
        self.assertEqual(invoice_item.end.day, 18)


class StorageModeTest(MarketplaceInvoiceBaseTest):
    def setUp(self):
        # Arrange
        super(StorageModeTest, self).setUp()
        fixture = package_fixtures.OpenStackFixture()
        tenant = fixture.openstack_tenant
        offering_component = marketplace_models.OfferingComponent.objects.create(
            offering=self.offering, type='gigabytes_gpfs'
        )

        marketplace_models.PlanComponent.objects.create(
            component=offering_component, plan=self.plan, price=10,
        )

        self.resource.scope = tenant
        self.resource.save()
        tenant.set_quota_limit('vcpu', 6)
        tenant.set_quota_limit('ram', 10 * 1024)
        tenant.set_quota_usage('storage', 30 * 1024)
        tenant.set_quota_usage('gigabytes_gpfs', 100 * 1024)

    def test_when_storage_mode_is_switched_to_dynamic_limits_are_updated(self):
        # Act
        with freeze_time('2019-09-20'):
            self.offering.plugin_options['storage_mode'] = STORAGE_MODE_DYNAMIC
            self.offering.save()

        # Assert
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits.get('cores'), 6)
        self.assertEqual(self.resource.limits.get('ram'), 10 * 1024)
        self.assertEqual(self.resource.limits.get('storage'), None)
        self.assertEqual(self.resource.limits.get('gigabytes_gpfs'), 100 * 1024)

        invoice_item = invoices_models.InvoiceItem.objects.filter(
            scope=self.resource
        ).last()
        self.assertTrue('102400 GB gpfs storage' in invoice_item.name)

    def test_when_storage_mode_is_switched_to_fixed_limits_are_updated(self):
        # Act
        with freeze_time('2019-09-20'):
            self.offering.plugin_options['storage_mode'] = STORAGE_MODE_FIXED
            self.offering.save()

        # Assert
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits.get('cores'), 6)
        self.assertEqual(self.resource.limits.get('ram'), 10 * 1024)
        self.assertEqual(self.resource.limits.get('storage'), 30 * 1024)
        self.assertEqual(self.resource.limits.get('gigabytes_gpfs'), None)

        invoice_item = invoices_models.InvoiceItem.objects.filter(
            scope=self.resource
        ).last()
        self.assertTrue('30 GB storage' in invoice_item.name)

    @mock.patch(
        'waldur_mastermind.marketplace_openstack.utils.import_limits_when_storage_mode_is_switched'
    )
    def test_when_storage_mode_is_not_switched_limits_are_not_updated(
        self, mocked_utils
    ):
        # Act
        with freeze_time('2019-09-20'):
            self.offering.plugin_options['FOO'] = 'BAR'
            self.offering.save()

        # Assert
        self.assertEqual(mocked_utils.call_count, 0)
