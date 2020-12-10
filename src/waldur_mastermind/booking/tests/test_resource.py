from functools import cached_property

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME


class MarketplaceFixture(structure_fixtures.ProjectFixture):
    @cached_property
    def offering(self) -> marketplace_models.Offering:
        return marketplace_factories.OfferingFactory(
            customer=self.customer, type=PLUGIN_NAME
        )

    @cached_property
    def plan(self) -> marketplace_models.PlanComponent:
        return marketplace_factories.PlanFactory(offering=self.offering)

    @cached_property
    def resource(self) -> marketplace_models.Resource:
        return marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=marketplace_models.Resource.States.CREATING,
            project=self.project,
            plan=self.plan,
        )

    @cached_property
    def order_item(self) -> marketplace_models.OrderItem:
        return marketplace_factories.OrderItemFactory(
            resource=self.resource,
            offering=self.offering,
            state=marketplace_models.OrderItem.States.EXECUTING,
        )


class OrderItemGetTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super(OrderItemGetTest, self).setUp()
        self.fixture = MarketplaceFixture()

    def test_get_resource_list(self):
        url = reverse('booking-resource-list')
        self.fixture.order_item
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            marketplace_models.Offering.objects.get(
                uuid=response.data[0]['offering_uuid']
            ).customer.get_owners()[0],
            self.fixture.owner,
        )

    def test_get_specific_fields(self):
        self.fixture.order_item.order.approved_by = self.fixture.staff
        self.fixture.order_item.order.save()
        self.fixture.resource.attributes['description'] = 'Description'
        self.fixture.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        url = reverse(
            'booking-resource-detail', kwargs={'uuid': self.fixture.resource.uuid.hex},
        )
        response = self.client.get(url)
        self.assertTrue(
            self.fixture.order_item.order.created_by.uuid.hex
            in response.data['created_by']
        )
        self.assertTrue(
            self.fixture.order_item.order.approved_by.uuid.hex
            in response.data['approved_by']
        )
        self.assertEqual('Description', response.data['description'])


class OrderItemAcceptTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super(OrderItemAcceptTest, self).setUp()
        self.fixture = MarketplaceFixture()
        self.fixture.order_item

    def accept(self, resource):
        self.client.force_authenticate(self.fixture.owner)
        url = '%s%s/accept/' % (reverse('booking-resource-list'), resource.uuid.hex,)
        return self.client.post(url)

    def test_owner_can_accept_his_resource(self):
        response = self.accept(self.fixture.resource)
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.OK
        )

        self.fixture.order_item.refresh_from_db()
        self.assertEqual(
            self.fixture.order_item.state, marketplace_models.OrderItem.States.DONE
        )

    def test_owner_cannot_accept_other_owners_resources(self):
        response = self.accept(MarketplaceFixture().resource)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)

    def test_when_order_item_is_accepted_resource_plan_period_is_created(self):
        self.accept(self.fixture.resource)
        self.assertTrue(
            marketplace_models.ResourcePlanPeriod.objects.filter(
                resource=self.fixture.resource, plan=self.fixture.plan
            ).exists()
        )


class OrderItemRejectTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super(OrderItemRejectTest, self).setUp()
        self.fixture = MarketplaceFixture()
        self.fixture.order_item

    def reject(self, resource):
        self.client.force_authenticate(self.fixture.owner)
        url = '%s%s/reject/' % (reverse('booking-resource-list'), resource.uuid.hex,)
        return self.client.post(url)

    def test_owner_can_reject_his_resource(self):
        response = self.reject(self.fixture.resource)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.TERMINATED
        )

        self.fixture.order_item.refresh_from_db()
        self.assertEqual(
            self.fixture.order_item.state,
            marketplace_models.OrderItem.States.TERMINATED,
        )

    def test_owner_cannot_reject_other_owners_resources(self):
        response = self.reject(MarketplaceFixture().resource)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)
