import logging
import re

from django.conf import settings
from django.db import transaction

from waldur_core.core.utils import is_uuid_like
from waldur_core.structure.models import Customer, Project, ProjectRole
from waldur_core.structure.utils import move_project
from waldur_mastermind.marketplace.models import (
    Offering,
    Order,
    OrderItem,
    Plan,
    Resource,
)
from waldur_mastermind.marketplace.tasks import approve_order, notify_order_approvers
from waldur_slurm.utils import sanitize_allocation_name

logger = logging.getLogger(__name__)


def get_internal_customer():
    customer_uuid = settings.WALDUR_HPC['INTERNAL_CUSTOMER_UUID']
    if not customer_uuid:
        logger.debug('Internal customer is not specified.')
        return
    if not is_uuid_like(customer_uuid):
        logger.warning('Internal customer UUID is invalid.')
        return
    try:
        return Customer.objects.get(uuid=customer_uuid)
    except Customer.DoesNotExist:
        logger.warning('Customer with UUID %s is not found', customer_uuid)
        return


def get_external_customer():
    customer_uuid = settings.WALDUR_HPC['EXTERNAL_CUSTOMER_UUID']
    if not customer_uuid:
        logger.debug('External customer is not specified.')
        return
    if not is_uuid_like(customer_uuid):
        logger.warning('External customer UUID is invalid.')
        return
    try:
        return Customer.objects.get(uuid=customer_uuid)
    except Customer.DoesNotExist:
        logger.warning('Customer with UUID %s is not found', customer_uuid)
        return


def get_offering():
    offering_uuid = settings.WALDUR_HPC['OFFERING_UUID']
    if not offering_uuid:
        logger.debug('Offering is not specified.')
        return
    if not is_uuid_like(offering_uuid):
        logger.warning('Offering UUID is invalid.')
        return
    try:
        offering = Offering.objects.get(uuid=offering_uuid)
    except Offering.DoesNotExist:
        logger.warning('Offering UUID %s is not found', offering_uuid)
        return

    if not offering.shared:
        logger.warning('Offering is not shared.')
        return

    return offering


def get_plan():
    plan_uuid = settings.WALDUR_HPC['PLAN_UUID']
    if not plan_uuid:
        logger.debug('Plan is not specified.')
        return
    if not is_uuid_like(plan_uuid):
        logger.warning('Plan UUID is invalid.')
        return
    try:
        return Plan.objects.get(uuid=plan_uuid)
    except Plan.DoesNotExist:
        logger.warning('Plan UUID %s is not found', plan_uuid)
        return


def get_or_create_project(customer, user, wrong_customer):
    try:
        return Project.objects.get(name=user.username, customer=customer)
    except Project.MultipleObjectsReturned:
        logger.warning('Multiple projects with the same name %s exist.', user.username)
        return
    except Project.DoesNotExist:
        try:
            # user has changed and has led to a change in INTERNAL/EXTERNAL decision
            project = Project.objects.get(name=user.username, customer=wrong_customer)
            move_project(project, customer)
            return project
        except Project.DoesNotExist:
            pass

        project = Project.objects.create(customer=customer, name=user.username)
        project.add_user(user, ProjectRole.ADMINISTRATOR)
        return project
    else:
        logger.warning('Projects with name %s already exists.', user.username)
        return


def get_or_create_order(project: Project, user, offering, plan, limits=None):
    limits = limits or {}
    order, order_created = Order.objects.get_or_create(project=project, created_by=user)

    if not order_created:
        if order.state in [Order.States.REQUESTED_FOR_APPROVAL, Order.States.EXECUTING]:
            return order, False
        if order.state == Order.States.DONE:
            order_item = order.items.first()
            if (
                order_item
                and order_item.state == OrderItem.States.DONE
                and order_item.resource
                and order_item.resource.state != Resource.States.ERRED
            ):
                return order, False

        order = Order.objects.create(project=project, created_by=user)

    order_item = OrderItem.objects.create(
        order=order,
        offering=offering,
        plan=plan,
        limits=limits,
        attributes={'name': sanitize_allocation_name(user.username)},
    )

    order_item.init_cost()
    order_item.save()

    order.init_total_cost()
    order.save()

    return order, True


def check_user(user, affiliations, email_patterns):
    if set(user.affiliations or []) & set(affiliations):
        return True

    return any(re.match(pattern, user.email) for pattern in email_patterns)


def is_internal_user(user):
    return check_user(
        user,
        settings.WALDUR_HPC['INTERNAL_AFFILIATIONS'],
        settings.WALDUR_HPC['INTERNAL_EMAIL_PATTERNS'],
    )


def is_external_user(user):
    if is_internal_user(user):
        return False

    return check_user(
        user,
        settings.WALDUR_HPC['EXTERNAL_AFFILIATIONS'],
        settings.WALDUR_HPC['EXTERNAL_EMAIL_PATTERNS'],
    )


def handle_new_user(sender, instance, created=False, **kwargs):
    if not settings.WALDUR_HPC['ENABLED']:
        return

    user = instance

    internal_customer = get_internal_customer()
    if not internal_customer:
        return

    external_customer = get_external_customer()
    if not external_customer:
        return

    offering = get_offering()
    if not offering:
        return

    plan = get_plan()
    if not plan:
        return

    if plan.offering != offering:
        logger.warning('Plan does not match offering.')
        return

    if is_internal_user(user):
        project = get_or_create_project(internal_customer, user, external_customer)

        if not project:
            return

        order, order_created = get_or_create_order(
            project,
            user,
            offering,
            plan,
            limits=settings.WALDUR_HPC['INTERNAL_LIMITS'],
        )

        if not order or not order_created:
            return

        approve_order(order, user)
        return

    if is_external_user(user):
        project = get_or_create_project(external_customer, user, internal_customer)

        if not project:
            return

        order, order_created = get_or_create_order(project, user, offering, plan)

        if not order or not order_created:
            return

        transaction.on_commit(lambda: notify_order_approvers.delay(order.uuid))
