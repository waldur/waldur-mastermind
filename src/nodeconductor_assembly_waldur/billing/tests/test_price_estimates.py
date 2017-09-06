from ddt import ddt, data
from django.contrib.contenttypes.models import ContentType
from freezegun import freeze_time
from rest_framework import status, test

from nodeconductor.core.tests.helpers import override_nodeconductor_settings
from nodeconductor.logging import models as logging_models
from nodeconductor.logging import tasks as logging_tasks
from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor.structure.tests import fixtures as structure_fixtures
from nodeconductor_assembly_waldur.packages.tests import fixtures as packages_fixtures
from nodeconductor_assembly_waldur.support import models as support_models
from nodeconductor_assembly_waldur.support.tests import fixtures as support_fixtures

from .. import exceptions, models
from . import factories


class PriceEstimateSignalsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def test_price_estimate_is_created_for_customer_by_signal(self):
        self.assertTrue(models.PriceEstimate.objects.filter(scope=self.fixture.customer).exists())

    def test_price_estimate_is_created_for_project_by_signal(self):
        self.assertTrue(models.PriceEstimate.objects.filter(scope=self.fixture.project).exists())

    def test_price_estimate_is_deleted_for_customer_by_signal(self):
        self.fixture.customer.delete()
        self.assertFalse(models.PriceEstimate.objects.filter(scope=self.fixture.customer).exists())

    def test_price_estimate_is_deleted_for_project_by_signal(self):
        self.fixture.project.delete()
        self.assertFalse(models.PriceEstimate.objects.filter(scope=self.fixture.project).exists())


@ddt
class PriceEstimateAPITest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_customer(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(total=100, limit=200)
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(structure_factories.CustomerFactory.get_url(self.fixture.customer))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['threshold'], 0)
        self.assertEqual(estimate['total'], 100)
        self.assertEqual(estimate['limit'], 200)

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_project(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.project).update(total=100, limit=200)
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(structure_factories.ProjectFactory.get_url(self.fixture.project))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['threshold'], 0)
        self.assertEqual(estimate['total'], 100)
        self.assertEqual(estimate['limit'], 200)


@freeze_time('2017-01-01 00:00:00')
class PriceEstimateInvoiceItemTest(test.APITransactionTestCase):
    def test_when_openstack_package_is_created_customer_total_is_updated(self):
        fixture = packages_fixtures.PackageFixture()
        package = fixture.openstack_package
        estimate = models.PriceEstimate.objects.get(scope=fixture.customer)
        self.assertEqual(estimate.total, package.template.price * 31)

    def test_when_openstack_package_is_created_project_total_is_updated(self):
        fixture = packages_fixtures.PackageFixture()
        package = fixture.openstack_package
        estimate = models.PriceEstimate.objects.get(scope=fixture.project)
        self.assertEqual(estimate.total, package.template.price * 31)

    def test_when_offering_is_created_customer_total_is_updated(self):
        fixture = support_fixtures.SupportFixture()
        offering = fixture.offering
        offering.state = support_models.Offering.States.OK
        offering.save()
        estimate = models.PriceEstimate.objects.get(scope=fixture.customer)
        self.assertEqual(estimate.total, offering.unit_price * 31)

    def test_when_offering_is_created_project_total_is_updated(self):
        fixture = support_fixtures.SupportFixture()
        offering = fixture.offering
        offering.state = support_models.Offering.States.OK
        offering.save()
        estimate = models.PriceEstimate.objects.get(scope=fixture.project)
        self.assertEqual(estimate.total, offering.unit_price * 31)


@freeze_time('2017-01-01 00:00:00')
class PriceEstimateLimitValidationTest(test.APITransactionTestCase):
    """
    If total cost of project and resource exceeds cost limit provision is disabled.
    """

    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()

    def test_if_resource_cost_exceeds_project_limit_provision_is_disabled(self):
        models.PriceEstimate.objects.filter(scope=self.fixture.project).update(limit=100)
        with self.assertRaises(exceptions.PriceEstimateLimitExceeded):
            self.create_resource(cost=300)

    def test_if_resource_cost_does_not_exceed_project_limit(self):
        models.PriceEstimate.objects.filter(scope=self.fixture.project).update(limit=100)
        self.create_resource(cost=10)

    def test_if_resource_cost_exceeds_customer_limit_provision_is_disabled(self):
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(limit=100)
        with self.assertRaises(exceptions.PriceEstimateLimitExceeded):
            self.create_resource(cost=300)

    def test_if_resource_cost_does_not_exceed_customer_limit(self):
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(limit=100)
        self.create_resource(cost=10)

    def create_resource(self, cost):
        offering = self.fixture.offering
        offering.unit_price = cost / 31.0
        offering.state = support_models.Offering.States.OK
        offering.save()


class PriceEstimateLimitTest(test.APITransactionTestCase):
    def setUp(self):
        super(PriceEstimateLimitTest, self).setUp()
        self.fixture = structure_fixtures.ProjectFixture()
        self.project_estimate = models.PriceEstimate.objects.get(scope=self.fixture.project)
        self.project_estimate_url = factories.PriceEstimateFactory.get_url(self.project_estimate)

        self.customer_estimate = models.PriceEstimate.objects.get(scope=self.fixture.customer)
        self.customer_estimate_url = factories.PriceEstimateFactory.get_url(self.customer_estimate)

    @override_nodeconductor_settings(OWNER_CAN_MODIFY_COST_LIMIT=True)
    def test_user_can_update_limit_if_it_is_allowed_by_configuration(self):
        self.client.force_authenticate(self.fixture.owner)
        new_limit = 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project_estimate.refresh_from_db()
        self.assertEqual(self.project_estimate.limit, new_limit)

    @override_nodeconductor_settings(OWNER_CAN_MODIFY_COST_LIMIT=False)
    def test_owner_cannot_update_limit_if_it_is_not_allowed_by_configuration(self):
        self.client.force_authenticate(self.fixture.owner)
        new_limit = 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    @override_nodeconductor_settings(OWNER_CAN_MODIFY_COST_LIMIT=False)
    def test_staff_can_update_limit_even_if_it_is_not_allowed_by_configuration(self):
        self.client.force_authenticate(self.fixture.staff)
        new_limit = 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_estimate.refresh_from_db()
        self.assertEqual(self.project_estimate.limit, new_limit)

    def test_it_is_not_possible_to_set_project_limit_larger_than_organization_limit(self):
        self.client.force_authenticate(self.fixture.staff)
        self.project_estimate.limit = 100
        self.project_estimate.save()
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(limit=self.project_estimate.limit)
        new_limit = self.project_estimate.limit + 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)
        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    def test_it_is_not_possible_to_increase_project_limit_if_all_customer_projects_limit_reached_customer_limit(self):
        self.client.force_authenticate(self.fixture.staff)
        self.project_estimate.limit = 10
        self.project_estimate.save()

        self.customer_estimate.limit = 100
        self.customer_estimate.save()

        new_project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        new_project_estimate = models.PriceEstimate.objects.get(scope=new_project)
        new_project_estimate.limit = self.customer_estimate.limit - self.project_estimate.limit
        new_project_estimate.save()

        # less than customer limit, projects total larger than customer limit
        new_limit = self.project_estimate.limit + 10

        response = self.client.put(self.project_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)
        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    def test_it_is_not_possible_to_set_organization_limit_lower_than_total_limit_of_its_projects(self):
        self.client.force_authenticate(self.fixture.staff)

        self.project_estimate.limit = 100
        self.project_estimate.save()

        new_limit = self.project_estimate.limit - 10
        response = self.client.put(self.customer_estimate_url, {'limit': new_limit})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)

        self.project_estimate.refresh_from_db()
        self.assertNotEqual(self.project_estimate.limit, new_limit)

    def test_it_is_possible_to_set_project_limit_if_customer_price_limit_is_default(self):
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
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(limit=10)
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
        self.assertEqual(threshold, response.data['billing_price_estimate']['threshold'])
