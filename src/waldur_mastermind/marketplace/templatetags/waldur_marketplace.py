from django import template
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _

from waldur_mastermind.marketplace import billing_mode, plugins
from waldur_mastermind.marketplace.enums import BillingTypes

register = template.Library()


@register.simple_tag
def get_invoice_item_component_amount(item, component):
    available_limits = plugins.manager.get_available_limits(
        component.component.offering.type
    )
    effective = billing_mode.resolve_component(component.component, component.plan)
    if item.limits and (
        effective.billing_type == BillingTypes.USAGE
        or component.component.type in available_limits
    ):
        builtin_components = plugins.manager.get_components(
            component.component.offering.type
        )
        component_factors = {c.type: c.factor for c in builtin_components}
        factor = component_factors.get(component.component.type, 1)
        limit = item.limits.get(component.component.type, 0)
        return limit / factor

    return component.amount


@register.simple_tag
def plan_details(plan):
    context = {"plan": plan, "components": []}

    for component in plan.components.all():
        offering_component = component.component
        if offering_component is None:
            continue
        effective = billing_mode.resolve_component(offering_component, plan)

        if effective.billing_type == BillingTypes.USAGE:
            continue

        amount = component.amount

        if effective.billing_type == BillingTypes.ONE_TIME:
            amount = _("one-time fee")

        if offering_component.billing_type == BillingTypes.ON_PLAN_SWITCH:
            amount = _("one-time on plan switch")

        context["components"].append(
            {
                "name": offering_component.name,
                "amount": amount,
                "price": component.price,
            }
        )

    plan_template = get_template("marketplace/marketplace_plan_template.txt").template
    return plan_template.render(template.Context(context, autoescape=False))
