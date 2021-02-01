import decimal

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.billing import exceptions, models
from waldur_mastermind.billing.tests import factories
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace_support.tests import (
    fixtures as marketplace_support_fixtures,
)
from waldur_mastermind.packages import views as packages_views
from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests import fixtures as packages_fixtures
from waldur_mastermind.packages.tests import utils as packages_utils
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
            total=100, limit=200
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['threshold'], 0)
        self.assertEqual(estimate['total'], 100)
        self.assertEqual(estimate['limit'], 200)

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_project(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.project).update(
            total=100, limit=200
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(
            structure_factories.ProjectFactory.get_url(self.fixture.project)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['threshold'], 0)
        self.assertEqual(estimate['total'], 100)
        self.assertEqual(estimate['limit'], 200)


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


@ddt
@freeze_time('2017-01-01')
class OfferingPriceEstimateLimitValidationTest(test.APITransactionTestCase):
    """
    If total cost of project and resource exceeds cost limit provision is disabled.
    """

    def setUp(self):
        self.fixture = marketplace_support_fixtures.MarketplaceSupportApprovedFixture()

    @data('project', 'customer')
    def test_if_resource_cost_exceeds_limit_provision_is_disabled(self, scope):
        models.PriceEstimate.objects.filter(scope=getattr(self.fixture, scope)).update(
            limit=100
        )
        with self.assertRaises(exceptions.PriceEstimateLimitExceeded):
            self.create_resource(cost=300)

    @data('project', 'customer')
    def test_if_resource_cost_does_not_exceed_limit_provision_is_allowed(self, scope):
        models.PriceEstimate.objects.filter(scope=getattr(self.fixture, scope)).update(
            limit=100
        )
        self.create_resource(cost=10)

    def create_resource(self, cost):
        plan_component = self.fixture.plan_component
        plan_component.price = cost
        plan_component.save()
        self.fixture.resource.set_state_ok()
        self.fixture.resource.save()


@ddt
@freeze_time('2017-01-01')
@override_plugin_settings(BILLING_ENABLED=True)
class PackagePriceEstimateLimitValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.package = self.fixture.openstack_package

    @data('project', 'customer')
    def test_if_extended_package_cost_does_not_exceed_limit_provision_is_allowed(
        self, scope
    ):
        models.PriceEstimate.objects.filter(scope=getattr(self.fixture, scope)).update(
            limit=100
        )

        response = self.extend_package(50)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        estimate = models.PriceEstimate.objects.get(scope=self.fixture.project)
        self.assertAlmostEqual(
            decimal.Decimal(estimate.total),
            decimal.Decimal(self.new_template.price * 31),
        )

    @data('project', 'customer')
    def test_if_extended_package_cost_exceeds_limit_provision_is_disabled(self, scope):
        models.PriceEstimate.objects.filter(scope=getattr(self.fixture, scope)).update(
            limit=100
        )

        response = self.extend_package(300)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

        estimate = models.PriceEstimate.objects.get(scope=self.fixture.project)
        self.assertAlmostEqual(
            decimal.Decimal(estimate.total),
            decimal.Decimal(self.package.template.price * 31),
        )

    def extend_package(self, total_price):
        self.new_template = packages_factories.PackageTemplateFactory(
            service_settings=self.fixture.openstack_service_settings
        )
        component_price = (
            total_price / 31.0 / len(self.new_template.get_required_component_types())
        )
        for component_type in self.new_template.get_required_component_types():
            self.new_template.components.filter(type=component_type).update(
                price=component_price, amount=1
            )

        view = packages_views.OpenStackPackageViewSet.as_view({'post': 'change'})
        response = common_utils.create_request(
            view,
            self.fixture.owner,
            {'template': self.new_template.uuid.hex, 'package': self.package.uuid.hex,},
        )

        if response.status_code == status.HTTP_202_ACCEPTED:
            packages_utils.run_openstack_package_change_executor(
                self.package, self.new_template
            )

        return response


class PriceEstimateLimitTest(test.APITransactionTestCase):
    def setUp(self):
        super(PriceEstimateLimitTest, self).setUp()
        self.fixture = structure_fixtures.ProjectFixture()

        self.project_estimate = models.PriceEstimate.objects.get(
            scope=self.fixture.project
        )
        self.project_estimate_url = factories.PriceEstimateFactory.get_url(
            self.project_estimate
        )

        self.customer_estimate = models.PriceEstimate.objects.get(
            scope=self.fixture.customer
        )
        self.customer_estimate_url = factories.PriceEstimateFactory.get_url(
            self.customer_estimate
        )

    def test_owner_can_update_project_limit(self):
        self.client.force_authenticate(self.fixture.owner)
        new_limit = 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project_estimate.refresh_from_db()
        self.assertEqual(self.project_estimate.limit, new_limit)

    def test_owner_cannot_update_customer_limit(self):
        self.client.force_authenticate(self.fixture.owner)
        new_limit = 10

        response = self.client.put(self.customer_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.customer_estimate.refresh_from_db()
        self.assertNotEqual(self.customer_estimate.limit, new_limit)

    def test_staff_can_update_customer_limit(self):
        self.client.force_authenticate(self.fixture.staff)
        new_limit = 10

        response = self.client.put(self.customer_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_estimate.refresh_from_db()
        self.assertEqual(self.customer_estimate.limit, new_limit)

    def test_it_is_not_possible_to_set_project_limit_larger_than_organization_limit(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)
        self.project_estimate.limit = 100
        self.project_estimate.save()
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(
            limit=self.project_estimate.limit
        )
        new_limit = self.project_estimate.limit + 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)
        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    def test_it_is_not_possible_to_increase_project_limit_if_all_customer_projects_limit_reached_customer_limit(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)
        self.project_estimate.limit = 10
        self.project_estimate.save()

        self.customer_estimate.limit = 100
        self.customer_estimate.save()

        new_project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        new_project_estimate = models.PriceEstimate.objects.get(scope=new_project)
        new_project_estimate.limit = (
            self.customer_estimate.limit - self.project_estimate.limit
        )
        new_project_estimate.save()

        # less than customer limit, projects total larger than customer limit
        new_limit = self.project_estimate.limit + 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)
        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    def test_it_is_not_possible_to_set_organization_limit_lower_than_total_limit_of_its_projects(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        self.project_estimate.limit = 100
        self.project_estimate.save()

        new_limit = self.project_estimate.limit - 10
        response = self.client.put(self.customer_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)

        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    def test_it_is_possible_to_set_project_limit_if_customer_price_limit_is_default(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)
        new_limit = self.project_estimate.limit + 100

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_estimate.refresh_from_db()
        self.assertEqual(self.project_estimate.limit, new_limit)

    def test_project_without_limits_do_not_affect_limit_validation(self):
        self.client.force_authenticate(self.fixture.staff)
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        models.PriceEstimate.objects.filter(scope=project).update(limit=-1)
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(
            limit=10
        )
        # 11 is an invalid limit as customer limit is 10.
        new_limit = 11

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)


class PriceEstimateThresholdApiTest(test.APITransactionTestCase):
    def setUp(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def test_staff_can_set_and_update_threshold_for_project(self):
        project = structure_factories.ProjectFactory()
        self.set_project_threshold(project, 200)
        self.set_project_threshold(project, 300)

    def set_project_threshold(self, project, threshold):
        project_url = structure_factories.ProjectFactory.get_url(project)

        estimate = models.PriceEstimate.objects.get(scope=project)
        url = factories.PriceEstimateFactory.get_url(estimate)
        response = self.client.put(url, {'threshold': threshold})
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)

        response = self.client.get(project_url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(
            threshold, response.data['billing_price_estimate']['threshold']
        )
