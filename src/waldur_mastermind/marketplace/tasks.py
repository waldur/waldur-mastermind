from __future__ import unicode_literals

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.marketplace.utils import process_order_item

from . import utils, models

logger = logging.getLogger(__name__)


def approve_order(order, user):
    order.approve()
    order.approved_by = user
    order.approved_at = timezone.now()
    order.save()

    serialized_order = core_utils.serialize_instance(order)
    serialized_user = core_utils.serialize_instance(user)
    transaction.on_commit(lambda: process_order.delay(serialized_order, serialized_user))
    transaction.on_commit(lambda: create_order_pdf.delay(order.pk))


@shared_task(name='marketplace.process_order')
def process_order(serialized_order, serialized_user):
    order = core_utils.deserialize_instance(serialized_order)
    user = core_utils.deserialize_instance(serialized_user)
    for item in order.items.all():
        process_order_item(item, user)


@shared_task(name='marketplace.create_screenshot_thumbnail')
def create_screenshot_thumbnail(uuid):
    screenshot = models.Screenshot.objects.get(uuid=uuid)
    utils.create_screenshot_thumbnail(screenshot)


@shared_task(name='marketplace.notify_order_approvers')
def notify_order_approvers(uuid):
    order = models.Order.objects.get(uuid=uuid)
    users = order.get_approvers()
    emails = [u.email for u in users if u.email]
    link_template = settings.WALDUR_MARKETPLACE['ORDER_LINK_TEMPLATE']

    context = {
        'order_url': link_template.format(project_uuid=order.project.uuid),
        'order': order,
        'site_name': settings.WALDUR_CORE['SITE_NAME'],
    }

    core_utils.broadcast_mail('marketplace', 'notification_approval', context, emails)


@shared_task(name='marketplace.notify_about_resource_change')
def notify_about_resource_change(event_type, context, resource_uuid):
    resource = models.Resource.objects.get(uuid=resource_uuid)
    emails = resource.project.get_users().values_list('email', flat=True)
    core_utils.broadcast_mail('marketplace', event_type, context, emails)


@shared_task
def create_order_pdf(order_id):
    order = models.Order.objects.get(pk=order_id)
    utils.create_order_pdf(order)


@shared_task
def create_pdf_for_all():
    for order in models.Order.objects.all():
        utils.create_order_pdf(order)


def filter_aggregate_by_scope(queryset, scope):
    scope_path = None

    if isinstance(scope, structure_models.Project):
        scope_path = 'resource__project'

    if isinstance(scope, structure_models.Customer):
        scope_path = 'resource__project__customer'

    if scope_path:
        queryset = queryset.filter(**{scope_path: scope})

    return queryset


def aggregate_reported_usage(start, end, scope):
    queryset = models.ComponentUsage.objects \
        .filter(date__gte=start, date__lte=end) \
        .exclude(component__parent=None)

    queryset = filter_aggregate_by_scope(queryset, scope)

    queryset = queryset.values('component__parent_id').annotate(total=Sum('usage'))

    return {
        row['component__parent_id']: row['total']
        for row in queryset
    }


def aggregate_fixed_usage(start, end, scope):
    queryset = models.ResourcePlanPeriod.objects.filter(start__gte=start, end__lte=end)
    queryset = filter_aggregate_by_scope(queryset, scope)

    queryset = queryset.values('plan__components__component__parent_id') \
        .annotate(total=Sum('plan__components__amount'))

    return {
        row['plan__components__component__parent_id']: row['total']
        for row in queryset
    }


def calculate_usage_for_scope(start, end, scope):
    reported_usage = aggregate_reported_usage(start, end, scope)
    fixed_usage = aggregate_fixed_usage(start, end, scope)
    # It needs to cover a case when a key is None because OfferingComponent.parent can be None.
    fixed_usage.pop(None, None)
    components = set(reported_usage.keys()) | set(fixed_usage.keys())
    content_type = ContentType.objects.get_for_model(scope)

    for component_id in components:
        models.CategoryComponentUsage.objects.update_or_create(
            content_type=content_type,
            object_id=scope.id,
            component_id=component_id,
            date=start,
            defaults={
                'reported_usage': reported_usage.get(component_id),
                'fixed_usage': fixed_usage.get(component_id),
            }
        )


@shared_task(name='waldur_mastermind.marketplace.calculate_usage_for_current_month')
def calculate_usage_for_current_month():
    start = invoice_utils.get_current_month_start()
    end = invoice_utils.get_current_month_end()
    scopes = []

    for customer in structure_models.Customer.objects.all():
        scopes.append(customer)
        for project in customer.projects.all():
            scopes.append(project)

    for scope in scopes:
        calculate_usage_for_scope(start, end, scope)
