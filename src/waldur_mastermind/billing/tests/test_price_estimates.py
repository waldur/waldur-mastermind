import decimal

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.billing import models
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace_support.tests import (
    fixtures as marketplace_support_fixtures,
)
from waldur_mastermind.packages import views as packages_views
from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests import fixtures as packages_fixtures
from waldur_mastermind.packages.tests.utils import override_plugin_settings


class PriceEstimateSignalsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def test_price_estimate_is_created_for_customer_by_signal(self):
        self.assertTrue(
            models.PriceEstimate.objects.filter(scope=self.fixture.customer).exists()
        )

    def test_price_estimate_is_created_for_project_by_signal(self):
        self.assertTrue(
            models.PriceEstimate.objects.filter(scope=self.fixture.project).exists()
        )

    def test_price_estimate_is_deleted_for_customer_by_signal(self):
        self.fixture.customer.delete()
        self.assertFalse(
            models.PriceEstimate.objects.filter(scope=self.fixture.customer).exists()
        )

    def test_price_estimate_is_deleted_for_project_by_signal(self):
        self.fixture.project.delete()
        self.assertFalse(
            models.PriceEstimate.objects.filter(scope=self.fixture.project).exists()
        )

    def test_price_estimate_for_project_is_updated_when_invoice_is_created(self):
        estimate = models.PriceEstimate.objects.get(scope=self.fixture.project)
        estimate.total = 100
        estimate.save()

        invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        estimate.refresh_from_db()
        self.assertEqual(estimate.total, 0)

    def test_price_estimate_for_customer_is_updated_when_invoice_is_created(self):
        estimate = models.PriceEstimate.objects.get(scope=self.fixture.customer)
        estimate.total = 100
        estimate.save()

        invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        estimate.refresh_from_db()
        self.assertEqual(estimate.total, 0)


@ddt
class PriceEstimateAPITest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @freeze_time('2017-11-01')
    def test_get_archive_price_estimate_for_customer(self):
        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(
            total=100
        )

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 100)

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer),
            {'year': 2017, 'month': 10},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 0)

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_customer(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(
            total=100
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 100)

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_project(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.project).update(
            total=100
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(
            structure_factories.ProjectFactory.get_url(self.fixture.project)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 100)


@ddt
@freeze_time('2017-01-01')
@override_plugin_settings(BILLING_ENABLED=True)
class PriceEstimateInvoiceItemTest(test.APITransactionTestCase):
    @data('project', 'customer')
    def test_when_openstack_package_is_created_total_is_updated(self, scope):
        fixture = packages_fixtures.PackageFixture()
        package = fixture.openstack_package
        estimate = models.PriceEstimate.objects.get(scope=getattr(fixture, scope))
        self.assertAlmostEqual(
            decimal.Decimal(estimate.total),
            decimal.Decimal(package.template.price * 31),
        )

    def test_when_openstack_package_is_extended_project_total_is_updated(self):
        fixture = packages_fixtures.PackageFixture()
        package = fixture.openstack_package
        new_template = packages_factories.PackageTemplateFactory(
            service_settings=fixture.openstack_service_settings
        )

        view = packages_views.OpenStackPackageViewSet.as_view({'post': 'change'})
        response = common_utils.create_request(
            view,
            fixture.owner,
            {'template': new_template.uuid.hex, 'package': package.uuid.hex,},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('project', 'customer')
    def test_when_offering_is_created_total_is_updated(self, scope):
        fixture = marketplace_support_fixtures.MarketplaceSupportApprovedFixture()
        resource = fixture.resource
        resource.set_state_ok()
        resource.save()
        estimate = models.PriceEstimate.objects.get(scope=getattr(fixture, scope))
        self.assertEqual(estimate.total, fixture.plan_component.price)
