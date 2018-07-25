from ddt import ddt, data
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .base_test import BaseCostTrackingTest
from .. import models


@ddt
class PriceEstimateListTest(BaseCostTrackingTest):
    def setUp(self):
        super(PriceEstimateListTest, self).setUp()

        self.link_price_estimate = factories.PriceEstimateFactory(
            year=2012, month=10, scope=self.service_project_link)
        self.project_price_estimate = factories.PriceEstimateFactory(scope=self.project, year=2015, month=7)

    @data('owner', 'manager', 'administrator')
    def test_user_can_see_price_estimate_for_his_project(self, user):
        self.client.force_authenticate(self.users[user])
        response = self.client.get(factories.PriceEstimateFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.project_price_estimate.uuid.hex, [obj['uuid'] for obj in response.data])

    @data('owner', 'manager', 'administrator')
    def test_user_cannot_see_price_estimate_for_not_his_project(self, user):
        other_price_estimate = factories.PriceEstimateFactory()

        self.client.force_authenticate(self.users[user])
        response = self.client.get(factories.PriceEstimateFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(other_price_estimate.uuid.hex, [obj['uuid'] for obj in response.data])

    def test_user_can_filter_price_estimate_by_scope(self):
        self.client.force_authenticate(self.users['owner'])
        response = self.client.get(
            factories.PriceEstimateFactory.get_list_url(),
            data={'scope': structure_factories.ProjectFactory.get_url(self.project)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.project_price_estimate.uuid.hex)

    def test_user_can_filter_price_estimates_by_date(self):
        self.client.force_authenticate(self.users['administrator'])
        response = self.client.get(
            factories.PriceEstimateFactory.get_list_url(),
            data={'date': '{}.{}'.format(self.link_price_estimate.year, self.link_price_estimate.month)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.link_price_estimate.uuid.hex)

    def test_user_can_filter_price_estimates_by_date_range(self):
        self.client.force_authenticate(self.users['manager'])
        response = self.client.get(
            factories.PriceEstimateFactory.get_list_url(),
            data={'start': '{}.{}'.format(self.link_price_estimate.year, self.link_price_estimate.month + 1),
                  'end': '{}.{}'.format(self.project_price_estimate.year, self.project_price_estimate.month + 1)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.project_price_estimate.uuid.hex)

    def test_user_receive_error_on_filtering_by_not_visible_for_him_object(self):
        data = {'scope': structure_factories.ProjectFactory.get_url()}

        self.client.force_authenticate(self.users['administrator'])
        response = self.client.get(factories.PriceEstimateFactory.get_list_url(), data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_define_children_visibility_depth(self):
        customer_price_estimate = factories.PriceEstimateFactory(scope=self.customer, year=2015, month=7)
        customer_price_estimate.children.add(self.project_price_estimate)
        spl_price_estimate = factories.PriceEstimateFactory(scope=self.service_project_link, year=2015, month=7)
        self.project_price_estimate.children.add(spl_price_estimate)

        self.client.force_authenticate(self.users['owner'])

        response = self.client.get(factories.PriceEstimateFactory.get_url(customer_price_estimate), data={'depth': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # with visibility depth 1 we want to see customer estimate children
        self.assertEqual(len(response.data['children']), 1)
        project_estimate_data = response.data['children'][0]
        self.assertEqual(project_estimate_data['uuid'], self.project_price_estimate.uuid.hex)
        # with visibility depth 1 we do not want to see grandchildren
        self.assertNotIn('children', project_estimate_data)


class PriceEstimateUpdateTest(BaseCostTrackingTest):
    def setUp(self):
        super(PriceEstimateUpdateTest, self).setUp()

        self.price_estimate = factories.PriceEstimateFactory(scope=self.service_project_link)
        self.valid_data = {
            'scope': structure_factories.TestServiceProjectLinkFactory.get_url(self.service_project_link),
            'total': 100,
            'details': {'ram': 50, 'disk': 50},
            'month': 7,
            'year': 2015,
        }

    def test_price_estimate_scope_cannot_be_updated(self):
        other_service_project_link = structure_factories.TestServiceProjectLinkFactory(project=self.project)
        self.valid_data['scope'] = structure_factories.TestServiceProjectLinkFactory.get_url(
            other_service_project_link)

        self.client.force_authenticate(self.users['staff'])
        response = self.client.patch(factories.PriceEstimateFactory.get_url(self.price_estimate), data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        reread_price_estimate = models.PriceEstimate.objects.get(id=self.price_estimate.id)
        self.assertNotEqual(reread_price_estimate.scope, other_service_project_link)

    def test_autocalculated_estimate_cannot_be_manually_updated(self):
        self.client.force_authenticate(self.users['staff'])
        response = self.client.patch(factories.PriceEstimateFactory.get_url(self.price_estimate), data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class PriceEstimateDeleteTest(BaseCostTrackingTest):
    def setUp(self):
        super(PriceEstimateDeleteTest, self).setUp()
        self.project_price_estimate = factories.PriceEstimateFactory(scope=self.project)

    def test_autocreated_price_estimate_cannot_be_deleted(self):
        self.client.force_authenticate(self.users['staff'])
        response = self.client.delete(factories.PriceEstimateFactory.get_url(self.project_price_estimate))

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class ScopeTypeFilterTest(BaseCostTrackingTest):
    def setUp(self):
        super(ScopeTypeFilterTest, self).setUp()
        resource = structure_factories.TestNewInstanceFactory(service_project_link=self.service_project_link)
        self.estimates = {
            'customer': models.PriceEstimate.objects.get(scope=self.customer),
            'service': models.PriceEstimate.objects.get(scope=self.service),
            'project': models.PriceEstimate.objects.get(scope=self.project),
            'service_project_link': models.PriceEstimate.objects.get(scope=self.service_project_link),
            'resource': models.PriceEstimate.objects.get(scope=resource),
        }

    def test_user_can_filter_price_estimate_by_scope_type(self):
        self.client.force_authenticate(self.users['owner'])
        for scope_type, estimate in self.estimates.items():
            response = self.client.get(
                factories.PriceEstimateFactory.get_list_url(),
                data={'scope_type': scope_type})

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), 1, response.data)
            self.assertEqual(response.data[0]['uuid'], estimate.uuid.hex)


class CustomerFilterTest(BaseCostTrackingTest):
    def setUp(self):
        super(CustomerFilterTest, self).setUp()
        resource = structure_factories.TestNewInstanceFactory()
        link = resource.service_project_link
        customer = link.customer
        project = link.project
        service = link.service

        scopes = {link, customer, project, service, resource}
        self.estimates = {models.PriceEstimate.objects.get(scope=scope) for scope in scopes}
        self.customer = customer

        resource2 = structure_factories.TestNewInstanceFactory()
        resource2_estimate = factories.PriceEstimateFactory(scope=resource2)
        resource2_estimate.create_ancestors()

    def test_user_can_filter_price_estimate_by_customer_uuid(self):
        self.client.force_authenticate(self.users['staff'])
        response = self.client.get(
            factories.PriceEstimateFactory.get_list_url(),
            data={'customer': self.customer.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({estimate['uuid'] for estimate in response.data},
                         {estimate.uuid.hex for estimate in self.estimates})
