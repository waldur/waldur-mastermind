from ddt import ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
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

    def test_user_is_staff_and_plans_are_not_matched_with_divisions(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_not_staff_and_plans_are_not_matched_with_divisions(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_staff_and_plans_are_matched_with_divisions(self):
        division = structure_factories.DivisionFactory()
        self.shared_plan.divisions.add(division)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_not_staff_and_plans_are_matched_with_divisions(self):
        division = structure_factories.DivisionFactory()
        self.shared_plan.divisions.add(division)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_user_is_owner_and_plan_and_customer_are_connected_the_same_division(self):
        division = structure_factories.DivisionFactory()
        self.shared_plan.divisions.add(division)
        self.customer.division = division
        self.customer.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_user_is_admin_and_plan_and_project_customer_are_connected_the_same_division(
        self,
    ):
        division = structure_factories.DivisionFactory()
        self.shared_plan.divisions.add(division)
        self.customer.division = division
        self.customer.save()
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_getting_plan_components_by_unauthorized_user(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

        self.shared_plan.divisions.add(structure_factories.DivisionFactory())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_filter_by_shared(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"shared": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]["plan_name"], self.shared_plan.name)

    def test_filter_by_archived(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        self.shared_plan.archived = True
        self.shared_plan.save()
        response = self.client.get(self.url, {"archived": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]["plan_name"], self.shared_plan.name)
