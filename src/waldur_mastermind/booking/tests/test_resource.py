from django.core import mail
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging import models as logging_models
from waldur_core.logging.tasks import process_event
from waldur_core.logging.tests.factories import EventFactory
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME


class OrderItemProcessedTest(test.APITransactionTestCase):
    def setUp(self):
        fixture_1 = structure_fixtures.CustomerFixture()
        fixture_1.owner
        self.staff = fixture_1.staff
        fixture_2 = structure_fixtures.CustomerFixture()
        fixture_2.owner
        offering_1 = marketplace_factories.OfferingFactory(
            customer=fixture_1.customer, type=PLUGIN_NAME
        )
        offering_2 = marketplace_factories.OfferingFactory(
            customer=fixture_2.customer, type=PLUGIN_NAME
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=offering_1, state=marketplace_models.Resource.States.CREATING
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            offering=offering_1,
            state=marketplace_models.OrderItem.States.EXECUTING,
        )
        self.resource_2 = marketplace_factories.ResourceFactory(offering=offering_2)
        self.owner = self.resource.offering.customer.get_owners()[0]
        self.client.force_authenticate(self.owner)

    def test_get_resource_list(self):
        url = reverse('booking-resource-list')
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            marketplace_models.Offering.objects.get(
                uuid=response.data[0]['offering_uuid']
            ).customer.get_owners()[0],
            self.owner,
        )

    def test_owner_can_accept_his_resource(self):
        url = '%s%s/accept/' % (
            reverse('booking-resource-list'),
            self.resource.uuid.hex,
        )
        response = self.client.post(url)
        self.resource.refresh_from_db()
        self.order_item.refresh_from_db()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )

    def test_notification(self):
        self.event_type = 'device_booking_is_accepted'
        self.event = EventFactory(event_type=self.event_type)
        logging_models.Feed.objects.create(
            scope=self.resource.project.customer, event=self.event
        )
        consumer_owner = structure_factories.UserFactory()
        self.resource.project.customer.add_user(
            consumer_owner, structure_models.CustomerRole.OWNER
        )
        email_hook = logging_models.EmailHook.objects.create(
            user=consumer_owner,
            email=consumer_owner.email,
            event_types=[self.event_type],
        )

        url = '%s%s/accept/' % (
            reverse('booking-resource-list'),
            self.resource.uuid.hex,
        )
        self.client.post(url)
        process_event(self.event.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [email_hook.email])

    def test_owner_cannot_accept_other_owners_resources(self):
        url = '%s%s/accept/' % (
            reverse('booking-resource-list'),
            self.resource_2.uuid.hex,
        )
        response = self.client.post(url)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)

    def test_owner_can_reject_his_resource(self):
        url = '%s%s/reject/' % (
            reverse('booking-resource-list'),
            self.resource.uuid.hex,
        )
        response = self.client.post(url)
        self.resource.refresh_from_db()
        self.order_item.refresh_from_db()
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.TERMINATED
        )

    def test_owner_cannot_reject_other_owners_resources(self):
        url = '%s%s/reject/' % (
            reverse('booking-resource-list'),
            self.resource_2.uuid.hex,
        )
        response = self.client.post(url)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)

    def test_get_specific_fields(self):
        self.order_item.order.approved_by = self.staff
        self.order_item.order.save()
        self.resource.attributes['description'] = 'Description'
        self.resource.save()
        url = reverse(
            'booking-resource-detail', kwargs={'uuid': self.resource.uuid.hex},
        )
        response = self.client.get(url)
        self.assertTrue(
            self.order_item.order.created_by.uuid.hex in response.data['created_by']
        )
        self.assertTrue(
            self.order_item.order.approved_by.uuid.hex in response.data['approved_by']
        )
        self.assertEqual('Description', response.data['description'])
