from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class OrderSubtypeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.staff
        self.offering = factories.OfferingFactory(project=self.project)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering, plan=self.plan
        )

    def test_order_creation_subtype_is_none(self):
        order = factories.OrderFactory(
            project=self.project,
            created_by=self.user,
            offering=self.offering,
            plan=self.plan,
            type=models.OrderTypes.CREATE,
            resource=self.resource,
        )
        url = factories.OrderFactory.get_url(order)
        self.client.force_authenticate(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["order_subtype"])

    def test_order_termination_subtype_is_none(self):
        order = factories.OrderFactory(
            project=self.project,
            created_by=self.user,
            offering=self.offering,
            plan=self.plan,
            type=models.OrderTypes.TERMINATE,
            resource=self.resource,
        )
        url = factories.OrderFactory.get_url(order)
        self.client.force_authenticate(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["order_subtype"])

    def test_order_switch_plan_subtype(self):
        new_plan = factories.PlanFactory(offering=self.offering)
        order = factories.OrderFactory(
            project=self.project,
            created_by=self.user,
            offering=self.offering,
            plan=new_plan,
            old_plan=self.plan,
            type=models.OrderTypes.UPDATE,
            resource=self.resource,
        )
        url = factories.OrderFactory.get_url(order)
        self.client.force_authenticate(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order_subtype"], "plan_switch")

    def test_order_renew_subtype(self):
        order = factories.OrderFactory(
            project=self.project,
            created_by=self.user,
            offering=self.offering,
            plan=self.plan,
            type=models.OrderTypes.UPDATE,
            resource=self.resource,
            attributes={"action": "renew"},
        )
        url = factories.OrderFactory.get_url(order)
        self.client.force_authenticate(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order_subtype"], "renew")

    def test_order_update_limits_subtype(self):
        order = factories.OrderFactory(
            project=self.project,
            created_by=self.user,
            offering=self.offering,
            plan=self.plan,
            type=models.OrderTypes.UPDATE,
            resource=self.resource,
            attributes={"old_limits": {"cpu": 1}},
        )
        url = factories.OrderFactory.get_url(order)
        self.client.force_authenticate(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order_subtype"], "update_limits")

    def test_order_update_options_subtype(self):
        order = factories.OrderFactory(
            project=self.project,
            created_by=self.user,
            offering=self.offering,
            plan=self.plan,
            type=models.OrderTypes.UPDATE,
            resource=self.resource,
            attributes={"new_options": {"opt": "val"}},
        )
        url = factories.OrderFactory.get_url(order)
        self.client.force_authenticate(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order_subtype"], "update_options")
