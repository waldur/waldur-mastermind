from django import template

from waldur_mastermind.marketplace import plugins

from .. import models

register = template.Library()


@register.simple_tag
def get_invoice_item_component_amount(item, component):
    if component.component.billing_type in (
        models.OfferingComponent.BillingTypes.ONE_TIME,
        models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH
    ):
        return component.component.amount

    available_limits = plugins.manager.get_available_limits(component.component.offering.type)
    if (component.component.billing_type == models.OfferingComponent.BillingTypes.USAGE or
            component.component.type in available_limits):
        builtin_components = plugins.manager.get_components(component.component.offering.type)
        component_factors = {c.type: c.factor for c in builtin_components}
        factor = component_factors.get(component.component.type, 1)
        if item.limits:
            limit = item.limits.get(component.component.type, 0)
            return limit / factor

    return component.amount
