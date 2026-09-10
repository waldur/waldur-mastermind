import logging
from typing import cast

from django.db import transaction

from waldur_autoprovisioning.models import Rule
from waldur_autoprovisioning.reconciliation import (
    reconcile_autoprovisioned_roles,
    resolve_customer,
)
from waldur_core.core.models import User
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.tasks import process_order_on_commit

logger = logging.getLogger(__name__)


def get_or_create_project(rule: Rule, user: User, customer: Customer) -> Project | None:
    """Find or create the project this rule provisions for the user.

    Role grants are deliberately *not* made here — they are issued by
    :func:`~waldur_autoprovisioning.reconciliation.reconcile_autoprovisioned_roles`
    once the project exists, so that every rule-issued grant is tagged with the
    rule's provenance through a single code path.
    """
    project = None
    project_name = rule.resolve_project_name(user)

    try:
        project = cast(
            Project,
            Project.available_objects.get(name=project_name, customer=customer),
        )
    except Project.MultipleObjectsReturned:
        logger.warning("Multiple projects with the same name %s exist.", project_name)
    except Project.DoesNotExist:
        project = cast(
            Project,
            Project.available_objects.create(customer=customer, name=project_name),
        )

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
    """Provision projects and orders when an account first appears.

    Only on creation. This used to run on every ``User.save()`` — including the
    attribute sync of an ordinary login — which meant editing a profile could
    materialise projects. Ongoing re-evaluation of the *roles* a rule asserts is
    handled by ``reconcile_autoprovisioned_roles``, called from the identity
    synchronisation paths where fresh claims actually arrive.
    """
    if not created:
        return

    provision_for_user(instance)


def handle_identity_synced(sender, user: User, created=False, **kwargs):
    """Reconcile rule-issued roles after identity data is refreshed.

    Skipped for a freshly created account: ``handle_new_user`` has already run
    the full provisioning pass, reconciliation included.
    """
    if created:
        return

    reconcile_autoprovisioned_roles(user)


def provision_for_user(user: User):
    """Run every matching rule for a user: projects, orders, then role grants."""
    rules = cast(list[Rule], Rule.get_objects_by_user_patterns(user))

    if not rules:
        return

    for rule in rules:
        resolution = resolve_customer(rule, user)
        if resolution.customer is None:
            logger.warning(
                "Rule '%s' (id=%s) matched user '%s' (id=%s) but resolves no "
                "organization: %s",
                rule.name,
                rule.pk,
                user.username,
                user.pk,
                resolution.block_reason,
            )
            continue

        if not rule.create_project:
            # Organization-level rule: the customer role is granted by the
            # reconciliation pass below.
            continue

        project = get_or_create_project(rule, user, resolution.customer)

        if not project:
            continue

        if rule.plan:
            plan = rule.plan
            attributes = rule.plan_attributes
            limits = rule.plan_limits

            order, order_created = get_or_create_order(
                project, user, plan.offering, plan, limits, attributes
            )

            # continue, not return: an already-provisioned order for one rule
            # must not stop the remaining rules — nor the role reconciliation
            # that follows the loop.
            if not order or not order_created:
                continue

            process_order_on_commit(order, user)

    # Grants happen last and in one place, so every rule-issued role carries the
    # rule's provenance and any project created above is already visible.
    reconcile_autoprovisioned_roles(user)
