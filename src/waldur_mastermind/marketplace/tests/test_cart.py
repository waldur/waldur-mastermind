from rest_framework import status, test

from waldur_core.structure.tests import fixtures, factories as structure_factories

from .. import models
from . import factories


class CartItemListTest(test.APITransactionTestCase):
    def setUp(self):
        self.cart_item = factories.CartItemFactory()

    def test_cart_item_renders_attributes(self):
        self.client.force_authenticate(self.cart_item.user)
        response = self.client.get(factories.CartItemFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('attributes' in response.data[0])


class CartSubmitTest(test.APITransactionTestCase):

    def submit(self, project):
        return self.client.post(factories.CartItemFactory.get_list_url('submit'), {
            'project': structure_factories.ProjectFactory.get_url(project)
        })

    def test_user_can_not_submit_shopping_cart_in_project_without_permissions(self):
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)

        self.client.force_authenticate(fixture.user)

        self.client.post(factories.CartItemFactory.get_list_url(), {
            'offering': factories.OfferingFactory.get_url(offering),
        })
        response = self.submit(fixture.project)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_gets_approved_if_all_offerings_are_private(self):
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=False,
            customer=fixture.customer
        )

        self.client.force_authenticate(fixture.staff)

        self.client.post(factories.CartItemFactory.get_list_url(), {
            'offering': factories.OfferingFactory.get_url(offering),
        })

        response = self.submit(fixture.project)
        self.assertEqual(response.data['state'], 'executing')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_cart_item_limits_are_propagated_to_order_item(self):
        limits = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }

        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        for key in limits.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.USAGE
            )

        payload = {
            'offering': factories.OfferingFactory.get_url(offering),
            'plan': factories.PlanFactory.get_url(plan),
            'limits': limits,
        }

        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.staff)

        url = factories.CartItemFactory.get_list_url()
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.submit(fixture.project)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order_item = models.OrderItem.objects.last()
        self.assertEqual(order_item.limits['cpu_count'], 5)

    def test_limits_are_not_allowed_for_components_with_disabled_quotas(self):
        limits = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }

        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        for key in limits.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.USAGE,
                disable_quotas=True,
            )

        payload = {
            'offering': factories.OfferingFactory.get_url(offering),
            'plan': factories.PlanFactory.get_url(plan),
            'limits': limits,
        }

        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.staff)

        url = factories.CartItemFactory.get_list_url()
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CartUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.cart_item = factories.CartItemFactory()

    def update(self, plan):
        self.client.force_authenticate(self.cart_item.user)
        return self.client.patch(factories.CartItemFactory.get_url(item=self.cart_item), {
            'plan': factories.PlanFactory.get_url(plan)
        })

    def test_update_cart_item(self):
        new_plan = factories.PlanFactory(offering=self.cart_item.offering)
        response = self.update(new_plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_validation(self):
        response = self.update(factories.PlanFactory())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
