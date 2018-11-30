from rest_framework import status, test

from waldur_core.structure.tests import fixtures, factories as structure_factories

from .. import models
from . import factories


class CartSubmitTest(test.APITransactionTestCase):
    def test_user_can_not_submit_shopping_cart_in_project_without_permissions(self):
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)

        self.client.force_authenticate(fixture.user)

        self.client.post(factories.CartItemFactory.get_list_url(), {
            'offering': factories.OfferingFactory.get_url(offering),
        })
        response = self.client.post(factories.CartItemFactory.get_list_url('submit'), {
            'project': structure_factories.ProjectFactory.get_url(fixture.project)
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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

        url = factories.CartItemFactory.get_list_url('submit')
        response = self.client.post(url, {
            'project': structure_factories.ProjectFactory.get_url(fixture.project)
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order_item = models.OrderItem.objects.last()
        self.assertEqual(order_item.limits['cpu_count'], 5)
