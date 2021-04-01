import logging

from django.conf import settings
from django.db import transaction

from waldur_core.core.utils import is_uuid_like
from waldur_core.structure.models import Customer, Project, ProjectRole
from waldur_mastermind.marketplace.models import Offering, Order, OrderItem, Plan
from waldur_mastermind.marketplace.tasks import approve_order, notify_order_approvers

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


def create_project(customer, user):
    try:
        Project.objects.get(name=user.username)
    except Project.MultipleObjectsReturned:
        logger.warning('Multiple projects with the same name %s exist.', user.username)
        return
    except Project.DoesNotExist:
        project = Project.objects.create(customer=customer, name=user.username)
        project.add_user(user, ProjectRole.ADMINISTRATOR)
        return project
    else:
        logger.warning('Projects with name %s already exists.', user.username)
        return


def create_order(project, user, offering, plan, limits=None):
    order = Order.objects.create(project=project, created_by=user)

    order_item = OrderItem.objects.create(
        order=order,
        offering=offering,
        plan=plan,
        limits=limits,
        attributes={'name': user.username},
    )

    order_item.init_cost()
    order_item.save()

    order.init_total_cost()
    order.save()

    return order


def handle_new_user(sender, instance, created=False, **kwargs):
    if not created:
        return

    if not settings.WALDUR_HPC['ENABLED']:
        return

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

    user = instance
    user_affiliations = set(user.affiliations or [])

    if user_affiliations & set(settings.WALDUR_HPC['INTERNAL_AFFILIATIONS']):
        project = create_project(internal_customer, user)
        if not project:
            return
        order = create_order(
            user,
            project,
            offering,
            plan,
            limits=settings.WALDUR_HPC['INTERNAL_LIMITS'],
        )
        if not order:
            return
        approve_order(order, user)
        return

    if user_affiliations & set(settings.WALDUR_HPC['EXTERNAL_AFFILIATIONS']):
        project = create_project(external_customer, user)
        if not project:
            return
        order = create_order(project, user, offering, plan)
        if not order:
            return
        transaction.on_commit(lambda: notify_order_approvers.delay(order.uuid))
