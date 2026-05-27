import logging
from decimal import Decimal

from constance import config
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import BillingTypes, OrderStates, OrderTypes

logger = logging.getLogger(__name__)


def estimated_monthly_cost(order: models.Order) -> Decimal:
    """Recurring monthly cost of an order, excluding one-time and switch fees.

    TERMINATE orders are treated as zero — terminating a resource removes
    future recurring billing.
    """
    if order.type == OrderTypes.TERMINATE:
        return Decimal("0")
    if not order.plan:
        return Decimal("0")
    return Decimal(str(order.plan.get_estimate(order.limits)))


def _has_usage_component(order: models.Order) -> bool:
    return order.offering.components.filter(billing_type=BillingTypes.USAGE).exists()


def _rule_creator_can_still_approve(rule: "models.ProjectOrderAutoApproval") -> bool:
    user = rule.created_by
    if user is None or not user.is_active:
        return False
    # has_permission returns True for staff regardless of scope.
    return has_permission(
        user, PermissionEnum.APPROVE_ORDER, rule.project
    ) or has_permission(user, PermissionEnum.APPROVE_ORDER, rule.project.customer)


def evaluate_auto_approval(
    order: models.Order,
) -> "models.ProjectOrderAutoApproval | None":
    """Return the rule that would auto-approve this order, or None.

    Pure function: makes no state changes. Locking and re-checks are the
    caller's responsibility (see try_apply_project_auto_approval).
    """
    if order.state != OrderStates.PENDING_CONSUMER:
        return None
    if order.plan is None and order.type != OrderTypes.TERMINATE:
        return None

    rule = models.ProjectOrderAutoApproval.objects.filter(
        project=order.project, enabled=True
    ).first()
    if rule is None:
        return None

    if _has_usage_component(order):
        return None

    try:
        monthly = estimated_monthly_cost(order)
    except Exception as exc:
        logger.warning(
            "Auto-approval skipped for order %s: cost estimate failed (%s).",
            order.uuid,
            exc,
        )
        return None

    if monthly > rule.monthly_cost_limit:
        return None

    return rule


def confirm_pending_terminate_order(order: models.Order, reviewer) -> models.Order:
    """Approve an existing pending-consumer terminate order.

    Used when a user with consumer approval rights submits terminate for a
    resource that already has a pending termination request.
    """
    if order.type != OrderTypes.TERMINATE:
        raise serializers.ValidationError(_("Order is not a termination order."))
    if order.state != OrderStates.PENDING_CONSUMER:
        raise serializers.ValidationError(_("Order is not pending consumer review."))

    with transaction.atomic():
        order = (
            models.Order.objects.select_for_update(of=("self",))
            .select_related("project", "offering", "plan")
            .get(pk=order.pk)
        )
        order.review_by_consumer(reviewer)
        transition_order_from_consumer_approval(order, reviewer)
    return order


def transition_order_from_consumer_approval(order: models.Order, reviewer) -> str:
    """Route a consumer-cleared order to its next state.

    Caller MUST have called order.review_by_consumer(reviewer) first and MUST
    hold a select_for_update lock on the order row inside an open atomic block.

    Returns one of: "pending_project", "pending_provider", "pending_start_date",
    "executing".
    """
    if order.project.start_date and order.project.start_date > timezone.now().date():
        order.state = OrderStates.PENDING_PROJECT
        order.save(update_fields=["state"])
        return "pending_project"

    if not utils.order_should_not_be_reviewed_by_provider(order):
        order.state = OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
        transaction.on_commit(
            lambda: tasks.notify_provider_about_pending_order.delay(order.uuid)
        )
        return "pending_provider"

    if (
        config.ENABLE_ORDER_START_DATE
        and order.start_date
        and order.start_date > timezone.now().date()
    ):
        order.state = OrderStates.PENDING_START_DATE
        order.save(update_fields=["state"])
        logger.info(
            "Order %s (%s) is pending start date %s.",
            order,
            order.id,
            order.start_date,
        )
        return "pending_start_date"

    order.set_state_executing()
    order.save(update_fields=["state"])
    logger.info(
        "Processing order %s (%s) after consumer approval, resource %s",
        order,
        order.id,
        order.resource,
    )
    tasks.process_order_on_commit(order, reviewer)
    return "executing"


def try_apply_project_auto_approval(order_pk: int) -> bool:
    """Lock the order and rule, re-check eligibility, transition if eligible.

    Designed to be invoked from a transaction.on_commit callback after order
    creation. Returns True if the order was auto-approved.
    """
    with transaction.atomic():
        try:
            order = (
                models.Order.objects.select_for_update(of=("self",))
                .select_related("project", "project__customer", "offering", "plan")
                .get(pk=order_pk)
            )
        except models.Order.DoesNotExist:
            return False

        if order.state != OrderStates.PENDING_CONSUMER:
            return False

        rule = evaluate_auto_approval(order)
        if rule is None:
            return False

        # Re-lock the rule with select_for_update and re-check enabled. This
        # guards against deletion or toggle between the signal-fire and now.
        rule = (
            models.ProjectOrderAutoApproval.objects.select_for_update(of=("self",))
            .select_related("created_by", "project__customer")
            .filter(pk=rule.pk, enabled=True)
            .first()
        )
        if rule is None:
            return False

        if not _rule_creator_can_still_approve(rule):
            logger.warning(
                "Auto-approval skipped for order %s: rule %s creator lost APPROVE_ORDER.",
                order.uuid,
                rule.uuid,
            )
            return False

        order.auto_approved_by_rule = rule
        order.auto_approved_cost_limit_snapshot = rule.monthly_cost_limit
        order.save(
            update_fields=[
                "auto_approved_by_rule",
                "auto_approved_cost_limit_snapshot",
            ]
        )
        order.review_by_consumer(rule.created_by)
        transition_order_from_consumer_approval(order, rule.created_by)
        return True
