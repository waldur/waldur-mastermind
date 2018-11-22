from django.conf import settings
from rest_framework import status

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest


class SupportOrderTest(BaseTest):

    def test_create_offering_if_order_item_is_approved(self):
        fixture = fixtures.ProjectFixture()
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []})

        order_item = marketplace_factories.OrderItemFactory(
            offering=offering,
            attributes={'name': 'item_name', 'description': 'Description'}
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.assertTrue(support_models.Offering.objects.filter(name='item_name').exists())

    def test_not_create_offering_if_marketplace_offering_is_not_support_type(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.staff
        self.offering = marketplace_factories.OfferingFactory()

        order_item = marketplace_factories.OrderItemFactory(offering=self.offering,
                                                            attributes={'name': 'item_name', 'description': '{}'})
        url = marketplace_factories.OrderFactory.get_url(order_item.order, 'set_state_executing')

        self.client.force_login(self.user)
        response = self.client.post(url)
        self.assertTrue(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_item_set_state_done(self):
        fixture = fixtures.ProjectFixture()
        offering = support_factories.OfferingFactory()
        resource = marketplace_factories.ResourceFactory(project=fixture.project, scope=offering)

        order_item = marketplace_factories.OrderItemFactory(resource=resource)
        order_item.set_state_executing()
        order_item.save()

        order_item.order.state = marketplace_models.Order.States.EXECUTING
        order_item.order.save()

        offering.state = support_models.Offering.States.OK
        offering.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

    def test_add_backlink_to_order_item_details_into_created_service_desk_ticket(self):
        fixture = fixtures.ProjectFixture()
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []})

        order_item = marketplace_factories.OrderItemFactory(
            offering=offering,
            attributes={'name': 'item_name', 'description': 'Description'}
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)
        self.assertTrue(support_models.Offering.objects.filter(name='item_name').exists())
        offering = support_models.Offering.objects.get(name='item_name')
        order_item_url = settings.ORDER_ITEM_LINK_TEMPLATE.format(order_item_uuid=order_item.uuid,
                                                                  customer_uuid=order_item.order.project.customer.uuid)
        self.assertTrue(order_item_url in offering.issue.description)
