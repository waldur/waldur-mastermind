import contextlib
import json
import logging

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.utils import process_order_item
from waldur_mastermind.support import models as support_models

logger = logging.getLogger(__name__)


class OrderTestException(Exception):
    pass


class OrderTest(object):
    def __init__(self, options, stdout_success):
        self.client = APIClient()
        self.stdout = stdout_success

        self.action = options.get('action')
        organization_name = options.get('organization')
        offering_uuid = options.get('offering')
        attributes_path = options.get('attributes')
        self.attributes = {}

        if attributes_path:
            with open(attributes_path) as attributes_file:
                attributes = attributes_file.read()
                self.attributes = json.loads(attributes)

        try:
            self.offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
        except marketplace_models.Offering.DoesNotExist:
            raise OrderTestException('Offering does not exist.')

        try:
            self.customer = structure_models.Customer.objects.get(name=organization_name)
            self.project = structure_models.Project.objects.get(customer=self.customer)
            self.owner = self.customer.get_owners().first()
            self.admin = self.project.get_users(role=structure_models.ProjectRole.ADMINISTRATOR).first()
        except structure_models.Customer.DoesNotExist:
            self.customer = structure_factories.CustomerFactory(name=organization_name)
            self.owner = structure_factories.UserFactory(username='test owner')
            self.customer.add_user(self.owner, structure_models.CustomerRole.OWNER)
            self.project = structure_factories.ProjectFactory(customer=self.customer)
            self.admin = structure_factories.UserFactory(username='test admin')
            self.project.add_user(self.admin, structure_models.ProjectRole.ADMINISTRATOR)
            self.stdout('Organization is created.')

        for i in range(1, 2 - self.offering.plans.count()):
            factories.PlanFactory(offering=self.offering)

        self.plan_1 = self.offering.plans.first()
        self.plan_2 = self.offering.plans.last()

    @property
    def order_item(self):
        order_item_queryset = marketplace_models.OrderItem.objects.filter(offering=self.offering,
                                                                          order__project=self.project)
        if order_item_queryset.exists():
            return order_item_queryset.last()

    @property
    def order(self):
        if self.order_item:
            return self.order_item.order

    @contextlib.contextmanager
    def mute_stdout(self):
        stdout = self.stdout
        self.stdout = lambda x: x
        try:
            yield {}
        finally:
            self.stdout = stdout

    def get_request_type(self):
        choices = marketplace_models.RequestTypeMixin.Types.CHOICES
        return filter(lambda x: x[0] == self.order_item.type, choices)[0][1].lower()

    def get_issue(self):
        if self.get_request_type() == 'create':
            return self.order_item.resource.scope.issue
        else:
            ct = ContentType.objects.get_for_model(marketplace_models.OrderItem)
            return support_models.Issue.objects.get(resource_content_type=ct,
                                                    resource_object_id=self.order_item.pk)

    def get_response(self, user, url_name, data=None, action=None, uuid=None, status_code=status.HTTP_200_OK):
        if uuid:
            url = 'http://localhost%s' % reverse(url_name, kwargs={'uuid': uuid})
        else:
            url = 'http://localhost%s' % reverse(url_name)

        if action:
            url += action + '/'

        self.client.force_authenticate(user)
        response = self.client.post(url, data)

        if response.status_code != status_code:
            raise OrderTestException('Request %s failed. %s' % (action, response.rendered_content))

        return response

    def approve_order(self):
        self.get_response(self.owner, 'marketplace-order-detail', action='approve', uuid=self.order.uuid)
        process_order_item(self.order_item, self.owner)

        self.stdout('A %s order has been approved.' % self.get_request_type())
        self.stdout('Order UUID: %s' % self.order.uuid)
        self.stdout('Request UUID: %s' % self.order_item.resource.scope.uuid)
        issue = self.get_issue()
        self.stdout('Issue UUID: %s, PK: %s' % (issue.uuid, issue.pk))

    def validate_order_done(self):
        if self.order.state != marketplace_models.Order.States.DONE:
            raise OrderTestException('An order is not done.')

    def validate_request_state(self, issue_resolved, request_state, request_type):
        if request_type == 'create' and issue_resolved and request_state == support_models.Offering.States.OK:
            return

        if request_type == 'create' and not issue_resolved and \
                request_state == support_models.Offering.States.TERMINATED:
            return

        if request_type == 'terminate' and issue_resolved and request_state is None:
            return

        if request_type == 'terminate' and not issue_resolved and request_state == support_models.Offering.States.OK:
            return

        if request_type == 'update' and request_state == support_models.Offering.States.OK:
            return

        raise OrderTestException('Request state is wrong.')

    def issue_info(self):
        if self.order.state == marketplace_models.Order.States.EXECUTING:
            self.stdout('STEP 2: resolve or cancel an issue.')
            self.stdout('A %s order UUID: %s' % (self.get_request_type(), self.order.uuid))
            self.stdout('Request UUID: %s' % self.order_item.resource.scope.uuid)
            issue = self.get_issue()
            self.stdout('Issue UUID: %s, PK: %s' % (issue.uuid, issue.pk))
            self.stdout('Please, resolve or cancel an issue.')

        if self.order.state == marketplace_models.Order.States.DONE:
            issue = self.get_issue()

            if issue.resolved is None:
                raise OrderTestException('An order is done, but the issue is not resolved or canceled.')
            elif issue.resolved:
                self.stdout('FINISH: A %s order has been resolved.' % self.get_request_type())
            else:
                self.stdout('FINISH: A %s order has been canceled.' % self.get_request_type())

            if self.order_item.resource.scope:
                self.stdout('Request state is: %s.' % self.order_item.resource.scope.state)
            else:
                self.stdout('Request with ID %s has been deleted.' % self.order_item.resource.object_id)

            self.stdout('Resource plan is: %s' % self.order_item.resource.plan.name)

            request_state = self.order_item.resource.scope and self.order_item.resource.scope.state
            self.validate_request_state(issue.resolved, request_state, self.get_request_type())

        if self.order.state == marketplace_models.Order.States.REJECTED:
            self.stdout('A %s order has been rejected.' % self.get_request_type())

        if self.order.state == marketplace_models.Order.States.ERRED:
            self.stdout('A %s order has failed.' % self.get_request_type())

    def create_request(self):
        self.stdout('STEP 1: Make an order.')
        data = {
            'project': 'http://localhost' + reverse('project-detail', kwargs={'uuid': self.project.uuid}),
            'items': [
                {
                    'offering': 'http://localhost' + reverse('marketplace-offering-detail',
                                                             kwargs={'uuid': self.offering.uuid}),
                    'attributes': self.attributes,
                    'limits': {},
                    'plan': 'http://localhost' + reverse('marketplace-plan-detail',
                                                         kwargs={'uuid': self.plan_1.uuid}),
                },
            ]
        }

        self.get_response(self.admin, 'marketplace-order-list', data=data, status_code=status.HTTP_201_CREATED)
        self.approve_order()

    def choice_step(func):
        def wrapped(self):
            if self.order_item:
                self.issue_info()
            else:
                func(self)

        return wrapped

    @choice_step
    def create(self):
        self.create_request()

    @choice_step
    def terminate(self):
        self.stdout('STEP 1: Make an order to terminate a resource.')
        with self.mute_stdout():
            self.create_request()
            self.order_item.resource.scope.issue.set_resolved()

        self.validate_order_done()
        self.get_response(self.admin, 'marketplace-resource-detail', action='terminate',
                          uuid=self.order_item.resource.uuid)
        self.approve_order()

    @choice_step
    def update(self):
        self.stdout('STEP 1: Make an order to switch plan of a resource.')
        with self.mute_stdout():
            self.create_request()
            self.order_item.resource.scope.issue.set_resolved()

        self.validate_order_done()
        self.stdout('Resource plan is: %s' % self.order_item.resource.plan.name)
        data = {
            'plan': 'http://localhost' + reverse('marketplace-plan-detail',
                                                 kwargs={'uuid': self.plan_2.uuid}),
        }
        self.get_response(self.admin, 'marketplace-resource-detail', data=data,
                          action='switch_plan', uuid=self.order_item.resource.uuid)

        self.approve_order()

    def delete(self):
        order_item_queryset = marketplace_models.OrderItem.objects.filter(offering=self.offering,
                                                                          order__project=self.project)
        if not order_item_queryset.exists():
            self.stdout('The order has already been deleted.')
            return

        for order_item in order_item_queryset.all():
            order_item.order.delete()

        self.stdout('Order has been deleted.')

    def run(self):
        getattr(self, self.action, lambda: None)()


class Command(BaseCommand):
    help = """Validate capability of a Marketplace plugin."""

    def add_arguments(self, parser):
        parser.add_argument('--offering', type=str, help='Offering UUID', required=True)
        parser.add_argument('--organization', type=str, help='Organization name', default='Test organization')
        parser.add_argument('--action', type=str, help='Action', default='create',
                            choices=['create', 'delete', 'terminate', 'update'])
        parser.add_argument('--attributes', type=str, help='Path to JSON file with order item attributes.', default='')

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                OrderTest(options, lambda m: self.stdout.write(self.style.SUCCESS(m))).run()
        except OrderTestException as e:
            self.stdout.write(self.style.ERROR(e.message))
