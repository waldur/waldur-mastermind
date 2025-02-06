import logging
import re

from django.db import transaction

from waldur_autoprovisioning.models import Rule
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.tasks import (
    process_order_on_commit,
)

logger = logging.getLogger(__name__)


def get_rules(user):
    rules = []
    for rule in Rule.objects.all():
        if set(user.affiliations or []) & set(rule.user_affiliations) or any(
            re.match(pattern, user.email) for pattern in rule.user_email_patterns
        ):
            rules.append(rule)

    return rules


def get_or_create_project(customer, user) -> Project | None:
    project = None

    try:
        project = Project.available_objects.get(name=user.username, customer=customer)

        if not project.has_user(user, ProjectRole.ADMIN):
            project.add_user(user, ProjectRole.ADMIN)

    except Project.MultipleObjectsReturned:
        logger.warning("Multiple projects with the same name %s exist.", user.username)
    except Project.DoesNotExist:
        project = Project.available_objects.create(
            customer=customer, name=user.username
        )
        project.add_user(user, ProjectRole.ADMIN)

    return project


def get_or_create_order(
    project: Project, user, offering, plan, limits=None, attributes=None
):
    limits = limits or {}
    attributes = attributes or {}

    order_ids = Order.objects.filter(offering=offering).values_list("id", flat=True)

    order: Order = (
        Order.objects.filter(
            project=project,
            created_by=user,
            state__in=(
                Order.States.DONE,
                Order.States.PENDING_CONSUMER,
                Order.States.PENDING_PROVIDER,
                Order.States.EXECUTING,
            ),
            id__in=order_ids,
        )
        .order_by("created")
        .last()
    )
    if order:
        if order.state in [
            Order.States.PENDING_CONSUMER,
            Order.States.PENDING_PROVIDER,
            Order.States.EXECUTING,
        ]:
            return order, False
        if order.state == Order.States.DONE:
            if order.resource.state != Resource.States.ERRED:
                return order, False

    name = marketplace_utils.generate_resource_name(project, offering)
    attributes.update({"name": name})

    with transaction.atomic():
        resource: Resource = Resource(
            project=project,
            offering=offering,
            plan=plan,
            limits=limits,
            attributes=attributes,
            name=name,
            state=Resource.States.CREATING,
        )
        resource.init_cost()
        resource.save()

        order: Order = Order(
            resource=resource,
            project=project,
            created_by=user,
            offering=offering,
            plan=plan,
            limits=limits,
            attributes=attributes,
            state=Order.States.EXECUTING,
        )

        order.init_cost()
        order.save()

    return order, True


def handle_new_user(sender, instance, created=False, **kwargs):
    user = instance

    rules: list = get_rules(user)

    if not rules:
        return

    for rule in rules:
        project: Project | None = get_or_create_project(rule.customer, user)

        if not project:
            return

        for rule_plan in rule.ruleplans_set.all():
            plan = rule_plan.plan
            attributes = rule_plan.attributes
            limits = rule_plan.limits

            order, order_created = get_or_create_order(
                project, user, plan.offering, plan, limits, attributes
            )

            if not order or not order_created:
                return

            process_order_on_commit(order, user)
