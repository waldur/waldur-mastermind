"""Changes to a provisioned resource that a decision outside the resource
itself grants: a new end date written directly, new limits through an update
order. Shared by the approval of a resource's change requests and by the
decision on a call's extension request, so both apply the same rules.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.structure import utils as structure_utils
from waldur_mastermind.marketplace import log, models, tasks, utils
from waldur_mastermind.marketplace.enums import OfferingStates, OrderTypes
from waldur_mastermind.marketplace.permissions import (
    order_is_held_for_purchase_order,
)


def apply_end_date(resource: models.Resource, end_date, user, message=None):
    """Write ``end_date`` onto the resource after checking it against the
    resource and its offering (``validate_end_date_for_resource``).

    The end date is a Waldur-side concept: moving it provisions and releases
    nothing, so it is written directly. ``user`` is recorded as the one who
    asked for the date. The caller holds a transaction.
    """
    utils.validate_end_date_for_resource(resource, end_date)
    resource.end_date = end_date
    resource.end_date_requested_by = user
    resource.save(update_fields=["end_date", "end_date_requested_by"])
    transaction.on_commit(
        lambda: tasks.notify_about_resource_termination.delay(
            resource.uuid.hex, user.uuid.hex, False
        )
    )
    log.log_resource_end_date_has_been_updated(
        resource,
        user,
        message
        or (
            "End date of marketplace resource %(resource_name)s has been updated"
            " through an approved change request."
            " End date: %(end_date)s."
            " User: %(user)s."
        ),
    )


def validate_granted_order(order: models.Order):
    """What a granted update order must pass when nobody places it through
    the order API: the consumer is not blocked, the project not expired, the
    offering still takes orders and the resource has no order pending.
    Access to the offering was settled when the resource was provisioned."""
    structure_utils.check_customer_blocked_or_archived(order.project.customer)
    structure_utils.check_project_end_date(order.project)
    if order.offering.state not in (OfferingStates.ACTIVE, OfferingStates.PAUSED):
        raise serializers.ValidationError(_("Offering is not available."))
    if utils.check_pending_order_exists(order.resource):
        raise serializers.ValidationError(
            _("Pending order for resource already exists.")
        )


def create_limit_update_order(
    resource: models.Resource,
    limits,
    user,
    validate_order=validate_granted_order,
    consumer_reviewed_by=None,
):
    """Create the update order that brings ``resource`` to ``limits``.

    The limits are validated against the offering first, which also sets
    the limits the server derives (``validate_limits``). ``validate_order``
    checks the unsaved order. With ``consumer_reviewed_by`` the consumer's
    review is recorded up front -- the decision that granted the limits was
    the consumer's -- unless a purchase order is still owed. The provider
    reviews the order as the offering routes any other one. The caller holds
    a transaction.
    """
    limits = utils.validate_limits(limits, resource.offering, resource)
    order = models.Order(
        project=resource.project,
        created_by=user,
        resource=resource,
        offering=resource.offering,
        plan=resource.plan,
        type=OrderTypes.UPDATE,
        limits=limits,
        attributes={"old_limits": resource.limits},
    )
    validate_order(order)
    if consumer_reviewed_by is not None and not order_is_held_for_purchase_order(order):
        order.consumer_reviewed_by = consumer_reviewed_by
        order.consumer_reviewed_at = timezone.now()
    order.init_cost()
    order.save()
    return order
