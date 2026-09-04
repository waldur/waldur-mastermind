"""Resolve how a component is billed under a given plan.

``OfferingComponent.billing_type`` is shared by every plan of an offering.
``Plan.billing_mode`` lets one plan bill the offering's *builtin* components
differently from another, so a single OpenStack offering can carry a
limit-based plan and a usage-based plan. Everything that decides how a
component is counted or invoiced must go through this module instead of
reading ``component.billing_type`` directly.

The module is imported from ``marketplace.models``, so it must not import
models, serializers or views at module level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Q

from waldur_mastermind.marketplace import plugins
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    BillingModes,
    BillingTypes,
    LimitPeriods,
)

if TYPE_CHECKING:
    from waldur_mastermind.marketplace import models

# Mirrors ``waldur_openstack.utils.is_valid_volume_type_name``; that module
# pulls in OpenStack models and cannot be imported while the app registry
# is still loading.
VOLUME_TYPE_COMPONENT_PREFIX = "gigabytes_"

# Deterministic measured units for OpenStack builtin components.
# Usage mode tracks component-hours; limit mode uses the raw quota unit.
OPENSTACK_USAGE_UNITS = {"cores": "core-hours"}
OPENSTACK_LIMIT_UNITS = {"cores": "cores"}
OPENSTACK_USAGE_DEFAULT_UNIT = "GB-hours"
OPENSTACK_LIMIT_DEFAULT_UNIT = "GB"


def is_builtin_component_type(offering_type: str, component_type: str) -> bool:
    """A builtin component is one the plugin registers for the offering type.

    For OpenStack the per-volume-type storage quotas are builtin too: they
    are created by the volume-type sync, not by the provider.
    """
    if component_type in plugins.manager.get_component_types(offering_type):
        return True
    return offering_type == OPENSTACK_TENANT_OFFERING and component_type.startswith(
        VOLUME_TYPE_COMPONENT_PREFIX
    )


def get_builtin_component_types(offering: models.Offering) -> set[str]:
    return {
        component.type
        for component in offering.components.all()
        if is_builtin_component_type(offering.type, component.type)
    }


def offering_has_builtin_components(offering: models.Offering) -> bool:
    return bool(plugins.manager.get_component_types(offering.type))


def measured_unit_for(
    offering_type: str, component_type: str, billing_type: str, default_unit: str
) -> str:
    """Unit shown for a builtin component under the given billing type."""
    if offering_type != OPENSTACK_TENANT_OFFERING:
        return default_unit
    if billing_type == BillingTypes.USAGE:
        return OPENSTACK_USAGE_UNITS.get(component_type, OPENSTACK_USAGE_DEFAULT_UNIT)
    return OPENSTACK_LIMIT_UNITS.get(component_type, OPENSTACK_LIMIT_DEFAULT_UNIT)


@dataclass(frozen=True)
class EffectiveComponent:
    """An offering component as it is billed under one plan."""

    component: models.OfferingComponent
    billing_type: str
    is_prepaid: bool
    limit_period: str
    measured_unit: str

    @property
    def type(self) -> str:
        return self.component.type

    @property
    def is_limit_like(self) -> bool:
        """Components whose quantity is a user-requested limit."""
        return self.billing_type == BillingTypes.LIMIT or (
            self.billing_type == BillingTypes.ONE_TIME and self.is_prepaid
        )


def _own_values(component: models.OfferingComponent) -> EffectiveComponent:
    return EffectiveComponent(
        component=component,
        billing_type=component.billing_type,
        is_prepaid=component.is_prepaid,
        limit_period=component.limit_period,
        measured_unit=component.measured_unit,
    )


def resolve_component(
    component: models.OfferingComponent, plan: models.Plan | None
) -> EffectiveComponent:
    """Effective billing of ``component`` under ``plan``.

    Without a plan, with an inheriting plan, or for a custom component the
    component's own values apply, so existing offerings behave as before.
    """
    if plan is None or plan.billing_mode == BillingModes.INHERIT:
        return _own_values(component)
    offering_type = component.offering.type
    if not is_builtin_component_type(offering_type, component.type):
        return _own_values(component)
    if plan.billing_mode == BillingModes.LIMIT:
        billing_type = BillingTypes.LIMIT
        limit_period = LimitPeriods.MONTH
    elif plan.billing_mode == BillingModes.USAGE:
        billing_type = BillingTypes.USAGE
        limit_period = component.limit_period
    else:
        return _own_values(component)
    return EffectiveComponent(
        component=component,
        billing_type=billing_type,
        is_prepaid=False,
        limit_period=limit_period,
        measured_unit=measured_unit_for(
            offering_type, component.type, billing_type, component.measured_unit
        ),
    )


class ResolvedPlan:
    """All components of an offering resolved for one plan (or no plan)."""

    def __init__(self, offering: models.Offering, plan: models.Plan | None):
        self.offering = offering
        self.plan = plan
        self.components: dict[str, EffectiveComponent] = {
            component.type: resolve_component(component, plan)
            for component in offering.components.all()
        }

    def get(self, component_type: str) -> EffectiveComponent | None:
        return self.components.get(component_type)

    @property
    def usage_types(self) -> set[str]:
        return {
            c.type
            for c in self.components.values()
            if c.billing_type == BillingTypes.USAGE
        }

    @property
    def limit_types(self) -> set[str]:
        return {
            c.type
            for c in self.components.values()
            if c.billing_type == BillingTypes.LIMIT
        }

    @property
    def limit_components(self) -> dict[str, models.OfferingComponent]:
        """Components whose quantity comes from the order limits."""
        return {
            c.type: c.component for c in self.components.values() if c.is_limit_like
        }

    @property
    def is_usage_based(self) -> bool:
        return bool(self.usage_types)

    @property
    def is_limit_based(self) -> bool:
        if not plugins.manager.can_update_limits(self.offering.type):
            return False
        return bool(self.limit_types)


def resolve_plan(plan: models.Plan) -> ResolvedPlan:
    return ResolvedPlan(plan.offering, plan)


def resolve_offering(offering: models.Offering) -> ResolvedPlan:
    """The inherit view of an offering: components as stored."""
    return ResolvedPlan(offering, None)


def resolve_for_resource(resource: models.Resource) -> ResolvedPlan:
    return ResolvedPlan(resource.offering, resource.plan)


def resolve_for_order(order: models.Order) -> ResolvedPlan:
    plan = order.plan
    if plan is None and order.resource is not None:
        plan = order.resource.plan
    return ResolvedPlan(order.offering, plan)


def resolve_for_plan_period(plan_period: models.ResourcePlanPeriod) -> ResolvedPlan:
    return ResolvedPlan(plan_period.resource.offering, plan_period.plan)


BILLING_LIMIT = "limit"
BILLING_USAGE = "usage"
BILLING_MIXED = "mixed"
BILLING_FIXED = "fixed"


def check_plan_billing_mode(
    offering: models.Offering, mode: str | None, plan: models.Plan | None = None
) -> str | None:
    """Why ``mode`` may not be set on a plan of ``offering``, or None.

    A mode other than inherit needs builtin components to act on, and it is
    frozen once resources use the plan: their open invoice items and usage
    rows follow the mode they were created under.
    """
    from waldur_mastermind.marketplace import models as marketplace_models

    if mode in (None, BillingModes.INHERIT) and plan is None:
        return None
    if mode not in (None, BillingModes.INHERIT) and not offering_has_builtin_components(
        offering
    ):
        return "Billing mode can be set only for offerings with builtin components."
    if (
        plan is not None
        and plan.pk
        and mode != plan.billing_mode
        and marketplace_models.Resource.objects.filter(plan=plan).exists()
    ):
        return "Billing mode cannot be changed while resources use this plan."
    return None


def describe_plan_billing(plan: models.Plan | None) -> str | None:
    """One word for how a plan bills: limit, usage, mixed or fixed."""
    if plan is None:
        return None
    resolved = resolve_plan(plan)
    has_limit = bool(resolved.limit_components)
    has_usage = resolved.is_usage_based
    if has_limit and has_usage:
        return BILLING_MIXED
    if has_usage:
        return BILLING_USAGE
    if has_limit:
        return BILLING_LIMIT
    return BILLING_FIXED


def describe_switch_consequence(
    old_billing: str | None, new_billing: str | None
) -> str:
    """Sentence for notifications explaining what a plan switch changes."""
    if old_billing == new_billing:
        return ""
    if new_billing == BILLING_USAGE:
        return (
            "The fee of the previous plan is charged for its current billing "
            "period (a monthly plan keeps this month's fee, a daily plan is "
            "charged for the days used). From now on the resource is billed for "
            "actual consumption; its quotas stay unchanged and cap usage instead "
            "of being billed."
        )
    if old_billing == BILLING_USAGE:
        return (
            "Usage accrued before the switch is invoiced at the previous plan's "
            "rates. From the switch the new plan's fee applies for its billing "
            "period (the whole month for a monthly plan, per day for a daily "
            "plan), based on the current limits."
        )
    return ""


def usage_offering_q(prefix: str = "") -> Q:
    """Offerings where at least one component or plan bills by usage."""
    return Q(**{f"{prefix}components__billing_type": BillingTypes.USAGE}) | Q(
        **{f"{prefix}plans__billing_mode": BillingModes.USAGE}
    )


def usage_resource_q() -> Q:
    """Resources whose plan bills by usage.

    A queryset approximation of :func:`resolve_for_resource`: a usage plan
    always qualifies, a limit plan never does for its builtin components, and
    an inheriting plan follows the stored component types. Custom usage
    components under a limit plan are missed; callers that need certainty
    resolve each resource afterwards.
    """
    return Q(plan__billing_mode=BillingModes.USAGE) | (
        Q(offering__components__billing_type=BillingTypes.USAGE)
        & ~Q(plan__billing_mode=BillingModes.LIMIT)
    )


def limit_offering_q(prefix: str = "") -> Q:
    return Q(**{f"{prefix}components__billing_type": BillingTypes.LIMIT}) | Q(
        **{f"{prefix}plans__billing_mode": BillingModes.LIMIT}
    )


def limit_resource_q() -> Q:
    """Resources whose plan bills on limits; see :func:`usage_resource_q`."""
    return Q(plan__billing_mode=BillingModes.LIMIT) | (
        Q(offering__components__billing_type=BillingTypes.LIMIT)
        & ~Q(plan__billing_mode=BillingModes.USAGE)
    )
