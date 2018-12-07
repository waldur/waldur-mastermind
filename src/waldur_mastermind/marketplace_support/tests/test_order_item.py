from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
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


class RequestCreateTest(BaseTest):

    def test_request_is_created_when_order_item_is_processed(self):
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

    def test_request_payload_is_validated(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.staff
        self.offering = marketplace_factories.OfferingFactory()

        order_item = marketplace_factories.OrderItemFactory(offering=self.offering,
                                                            attributes={'name': 'item_name', 'description': '{}'})
        url = marketplace_factories.OrderFactory.get_url(order_item.order, 'approve')

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

        order_item.resource.refresh_from_db()
        self.assertEqual(order_item.resource.state, marketplace_models.Resource.States.OK)

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

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
        link_template = settings.WALDUR_MARKETPLACE['ORDER_ITEM_LINK_TEMPLATE']
        order_item_url = link_template.format(order_item_uuid=order_item.uuid,
                                              project_uuid=order_item.order.project.uuid)
        self.assertTrue(order_item_url in offering.issue.description)


class RequestDeleteTest(BaseTest):
    def setUp(self):
        super(RequestDeleteTest, self).setUp()
        self.fixture = fixtures.ProjectFixture()
        self.offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        self.request = support_factories.OfferingFactory(state=support_models.Offering.States.TERMINATED)
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            scope=self.request,
            offering=self.offering,
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_request_is_deleted(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.request.refresh_from_db)
