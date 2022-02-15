from ddt import ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures

from . import factories


@ddt
class PlanComponentsGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.shared_offering = factories.OfferingFactory(
            customer=self.customer,
            shared=True,
        )
        self.shared_plan = factories.PlanFactory(offering=self.shared_offering)
        self.shared_offering_component = factories.OfferingComponentFactory(
            offering=self.shared_offering
        )
        self.shared_plan_component = factories.PlanComponentFactory(
            plan=self.shared_plan, component=self.shared_offering_component
        )

        self.privat_offering = factories.OfferingFactory(
            customer=self.customer,
            shared=False,
        )
        self.privat_plan = factories.PlanFactory(offering=self.privat_offering)
        self.privat_offering_component = factories.OfferingComponentFactory(
            offering=self.privat_offering
        )
        self.privat_plan_component = factories.PlanComponentFactory(
            plan=self.privat_plan, component=self.privat_offering_component
        )

        self.url = factories.PlanComponentFactory.get_list_url()

    def test_getting_plan_components_by_authorized_user(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_getting_plan_components_by_unauthorized_user(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_filter_by_shared(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {'shared': True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]['plan_name'], self.shared_plan.name)

    def test_filter_by_archived(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        self.shared_plan.archived = True
        self.shared_plan.save()
        response = self.client.get(self.url, {'archived': True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]['plan_name'], self.shared_plan.name)
