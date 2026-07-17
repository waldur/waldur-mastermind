import logging
from typing import cast

from django.db import transaction

from waldur_autoprovisioning.models import Rule
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.tasks import process_order_on_commit

logger = logging.getLogger(__name__)


def get_or_create_project(rule: Rule, user: User) -> Project | None:
    project = None
    project_role = rule.project_role or ProjectRole.ADMIN

    project_name = rule.resolve_project_name(user)

    if not rule.use_user_organization_as_customer_name:
        if not rule.customer:
            logger.warning(
                "Rule '%s' (id=%s) has no customer configured and 'use_user_organization_as_customer_name' is disabled.",
                rule.name,
                rule.pk,
            )
            return

        customer = rule.customer
    else:
        if not user.should_protect_user_details:
            logger.warning(
                "Rule '%s' (id=%s) requires using user's organization, but user '%s' (id=%s) is not marked as protected (should_protect_user_details is False).",
                rule.name,
                rule.pk,
                user.username,
                user.pk,
            )
            return

        if not user.organization:
            logger.warning(
                "User '%s' (id=%s) has no organization claim; cannot resolve Customer when 'use_user_organization_as_customer_name' is enabled.",
                user.username,
                user.pk,
            )
            return

        customers = Customer.objects.filter(name=user.organization)

        if not customers:
            logger.warning(
                "No Customer found with name='%s' for user '%s' (id=%s) from organization claim.",
                user.organization,
                user.username,
                user.pk,
            )
            return
        elif customers.count() > 1:
            logger.warning(
                "Multiple Customers found with name='%s' for user '%s' (id=%s) from organization claim.",
                user.organization,
                user.username,
                user.pk,
            )
            return
        else:
            customer = customers.first()

    try:
        project = cast(
            Project,
            Project.available_objects.get(name=project_name, customer=customer),
        )

        if not project.has_user(user, project_role):
            project.add_user_or_skip(user, project_role)

    except Project.MultipleObjectsReturned:
        logger.warning("Multiple projects with the same name %s exist.", project_name)
    except Project.DoesNotExist:
        project = cast(
            Project,
            Project.available_objects.create(customer=customer, name=project_name),
        )
        project.add_user_or_skip(user, project_role)

    return project


def get_or_create_order(
    project: Project, user, offering, plan, limits=None, attributes: dict | None = None
):
    limits = limits or {}
    attributes = attributes or {}

    order_ids = Order.objects.filter(offering=offering).values_list("id", flat=True)

    order = (
        Order.objects.filter(
            project=project,
            created_by=user,
            state__in=(
                OrderStates.DONE,
                OrderStates.PENDING_CONSUMER,
                OrderStates.PENDING_PROVIDER,
                OrderStates.EXECUTING,
            ),
            id__in=order_ids,
        )
        .order_by("created")
        .last()
    )
    if order:
        if order.state in [
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.EXECUTING,
        ]:
            return order, False
        if order.state == OrderStates.DONE:
            if order.resource.state != ResourceStates.ERRED:
                return order, False

    name = marketplace_utils.generate_resource_name(project, offering)
    attributes.update({"name": name})

    with transaction.atomic():
        resource = Resource(
            project=project,
            offering=offering,
            plan=plan,
            limits=limits,
            attributes=attributes,
            name=name,
            state=ResourceStates.CREATING,
        )
        resource.init_cost()
        resource.save()

        order = Order(
            resource=resource,
            project=project,
            created_by=user,
            offering=offering,
            plan=plan,
            limits=limits,
            attributes=attributes,
            state=OrderStates.EXECUTING,
        )

        order.init_cost()
        order.save()

    return order, True


def handle_new_user(sender, instance: User, created=False, **kwargs):
    """Create project and order for new user based on autoprovisioning rules."""
    user = instance

    rules = cast(list[Rule], Rule.get_objects_by_user_patterns(user))

    if not rules:
        return

    for rule in rules:
        project = get_or_create_project(rule, user)

        if not project:
            continue

        if rule.plan:
            plan = rule.plan
            attributes = rule.plan_attributes
            limits = rule.plan_limits

            order, order_created = get_or_create_order(
                project, user, plan.offering, plan, limits, attributes
            )

            if not order or not order_created:
                return

            process_order_on_commit(order, user)
