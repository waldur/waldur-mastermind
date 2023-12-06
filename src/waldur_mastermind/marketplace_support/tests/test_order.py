from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support.tests.fixtures import (
    MarketplaceSupportApprovedFixture,
    SupportFixture,
)


class RequestCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = SupportFixture()
        self.offering = self.fixture.offering
        self.offering.state = marketplace_models.Offering.States.ACTIVE
        self.offering.save()
        self.plan = self.fixture.plan

    def test_create_order(self):
        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def create_order(self, user='staff'):
        project_url = structure_factories.ProjectFactory.get_url(self.fixture.project)
        offering_url = marketplace_factories.OfferingFactory.get_public_url(
            self.offering
        )
        plan_url = marketplace_factories.PlanFactory.get_public_url(self.plan)

        attributes = dict(
            name='My first request-based item',
        )

        payload = {
            'project': project_url,
            'offering': offering_url,
            'plan': plan_url,
            'attributes': attributes,
        }

        self.client.force_login(getattr(self.fixture, user))
        url = marketplace_factories.OrderFactory.get_list_url()
        return self.client.post(url, payload)


class RequestUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = MarketplaceSupportApprovedFixture()
        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()
        self.new_plan = marketplace_factories.PlanFactory(
            offering=self.fixture.marketplace_offering
        )

    def test_create_order(self):
        response = self.switch_plan(self.fixture.staff, self.resource, self.new_plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def switch_plan(self, user, resource, plan):
        self.client.force_authenticate(user)
        url = marketplace_factories.ResourceFactory.get_url(resource, 'switch_plan')
        payload = {'plan': marketplace_factories.PlanFactory.get_public_url(plan)}
        return self.client.post(url, payload)
