from decimal import Decimal

import datetime
from ddt import data, ddt
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from freezegun import freeze_time
from rest_framework import status

from waldur_core.core.utils import month_end
from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import fixtures
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.plugins import manager
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

    def test_order_item_set_state_done_if_offering_set_state_ok(self):
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

    def test_order_item_set_state_erred_if_offering_terminated(self):
        fixture = fixtures.ProjectFixture()
        offering = support_factories.OfferingFactory()
        resource = marketplace_factories.ResourceFactory(project=fixture.project, scope=offering)

        order_item = marketplace_factories.OrderItemFactory(resource=resource)
        order_item.set_state_executing()
        order_item.save()

        order_item.order.state = marketplace_models.Order.States.EXECUTING
        order_item.order.save()

        offering.state = support_models.Offering.States.TERMINATED
        offering.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.ERRED)

        order_item.resource.refresh_from_db()
        self.assertEqual(order_item.resource.state, marketplace_models.Resource.States.ERRED)

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

    def submit_order_item(self):
        fixture = fixtures.ProjectFixture()
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []})

        order_item = marketplace_factories.OrderItemFactory(
            offering=offering,
            attributes={'name': 'item_name', 'description': 'Description'}
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)
        return order_item

    def test_add_backlink_to_order_item_details_into_created_service_desk_ticket(self):
        order_item = self.submit_order_item()
        self.assertTrue(support_models.Offering.objects.filter(name='item_name').exists())
        offering = support_models.Offering.objects.get(name='item_name')
        link_template = settings.WALDUR_MARKETPLACE['ORDER_ITEM_LINK_TEMPLATE']
        order_item_url = link_template.format(order_item_uuid=order_item.uuid,
                                              project_uuid=order_item.order.project.uuid)
        self.assertTrue(order_item_url in offering.issue.description)

    def test_resource_name_is_propagated(self):
        self.submit_order_item()
        offering = support_models.Offering.objects.get(name='item_name')
        resource = marketplace_models.Resource.objects.get(scope=offering)
        self.assertEqual(resource.attributes['name'], 'item_name')


@freeze_time('2019-01-01')
class RequestActionBaseTest(BaseTest):
    def setUp(self):
        super(RequestActionBaseTest, self).setUp()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

        self.user = self.fixture.staff
        self.offering = marketplace_factories.OfferingFactory(
            state=marketplace_models.Offering.States.ACTIVE,
            type=PLUGIN_NAME)

        self.request = support_factories.OfferingFactory(template=self.offering.scope, project=self.project)
        self.current_plan = marketplace_factories.PlanFactory(offering=self.offering, unit_price=10)
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED)

        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.current_plan,
            component=self.offering_component
        )
        self.resource = marketplace_factories.ResourceFactory(
            project=self.project,
            scope=self.request,
            offering=self.offering,
            plan=self.current_plan,
        )
        self.resource.scope.state = support_models.Offering.States.OK
        self.resource.scope.save()

        self.success_issue_status = 'ok'
        support_factories.IssueStatusFactory(
            name=self.success_issue_status,
            type=support_models.IssueStatus.Types.RESOLVED)

        self.error_issue_status = 'error'
        support_factories.IssueStatusFactory(
            name=self.error_issue_status,
            type=support_models.IssueStatus.Types.CANCELED)

        self.start = datetime.datetime.now()


@ddt
class RequestDeleteTest(RequestActionBaseTest):
    def test_success_terminate_resource_if_issue_is_resolved(self):
        order_item = self.get_order_item(self.success_issue_status)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.mock_get_active_backend.call_count, 1)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)
        self.assertRaises(ObjectDoesNotExist, self.request.refresh_from_db)

    def test_fail_termination_order_item_if_issue_is_canceled(self):
        order_item = self.get_order_item(self.error_issue_status)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)

    @data('staff', 'owner', 'admin', 'manager')
    def test_terminate_operation_is_available(self, user):
        user = getattr(self.fixture, user)
        response = self.request_resource_termination(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_terminate_operation_is_not_available(self, user):
        user = getattr(self.fixture, user)
        response = self.request_resource_termination(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def request_resource_termination(self, user=None):
        user = user or self.user
        url = marketplace_factories.ResourceFactory.get_url(resource=self.resource, action='terminate')
        self.client.force_authenticate(user)
        return self.client.post(url)

    def get_order_item(self, issue_status):
        self.request_resource_termination()
        order = marketplace_models.Order.objects.get(project=self.project)
        order_item = order.items.first()
        if order_item.order.state != marketplace_models.Order.States.EXECUTING:
            order_item.order.approve()
            order_item.order.save()
        manager.process(order_item, self.user)

        order_item_content_type = ContentType.objects.get_for_model(order_item)
        issue = support_models.Issue.objects.get(resource_object_id=order_item.id,
                                                 resource_content_type=order_item_content_type)
        issue.status = issue_status
        issue.save()
        return marketplace_models.OrderItem.objects.first()


@ddt
class RequestSwitchPlanTest(RequestActionBaseTest):
    def setUp(self):
        super(RequestSwitchPlanTest, self).setUp()
        self.plan = marketplace_factories.PlanFactory(offering=self.offering, unit_price=50)
        marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=Decimal(50)
        )

    def test_success_switch_plan_if_issue_is_resolved(self):
        order_item = self.get_order_item(self.success_issue_status)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.mock_get_active_backend.call_count, 1)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.plan, self.plan)

    def test_fail_switch_plan_if_issue_is_fail(self):
        order_item = self.get_order_item(self.error_issue_status)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.ERRED)
        self.assertEqual(self.resource.plan, self.current_plan)

    def test_new_price_is_copied_if_plan_has_been_switched(self):
        self.get_order_item(self.success_issue_status)
        self.resource.refresh_from_db()
        self.resource.scope.refresh_from_db()
        self.assertEqual(self.resource.plan.unit_price, self.resource.scope.unit_price)

    @freeze_time('2019-01-15')
    def test_switch_invoice_item_if_plan_switched(self):
        self.get_order_item(self.success_issue_status)
        new_start = datetime.datetime.now()
        end = month_end(new_start)
        self.assertTrue(invoices_models.GenericInvoiceItem.objects.filter(
            scope=self.request,
            project=self.project,
            unit_price=Decimal(10),
            start=self.start,
            end=new_start,
        ).exists())
        self.assertTrue(invoices_models.GenericInvoiceItem.objects.filter(
            scope=self.request,
            project=self.project,
            unit_price=Decimal(50),
            start=new_start,
            end=end,
        ).exists())

    @data('staff', 'owner', 'admin', 'manager')
    def test_switch_plan_operation_is_available(self, user):
        user = getattr(self.fixture, user)
        response = self.request_switch_plan(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_resource_is_not_available(self, user):
        user = getattr(self.fixture, user)
        response = self.request_switch_plan(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_resource_state_validation(self):
        self.resource.set_state_updating()
        self.resource.save()
        response = self.request_switch_plan()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_plan_validation(self):
        response = self.request_switch_plan(add_payload={'plan': marketplace_factories.PlanFactory.get_url()})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def request_switch_plan(self, user=None, add_payload=None):
        user = user or self.user
        url = marketplace_factories.ResourceFactory.get_url(resource=self.resource, action='switch_plan')
        payload = {
            'plan': marketplace_factories.PlanFactory.get_url(self.plan)
        }

        if add_payload:
            payload.update(add_payload)

        self.client.force_authenticate(user)
        return self.client.post(url, payload)

    def get_order_item(self, issue_status):
        self.request_switch_plan()
        order = marketplace_models.Order.objects.get(project=self.project)
        order_item = order.items.first()
        manager.process(order_item, self.user)

        order_item_content_type = ContentType.objects.get_for_model(order_item)
        issue = support_models.Issue.objects.get(resource_object_id=order_item.id,
                                                 resource_content_type=order_item_content_type)
        issue.status = issue_status
        issue.save()
        order_item.refresh_from_db()
        return order_item
