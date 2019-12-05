from django import template
from django.conf import settings

from waldur_mastermind.marketplace import plugins

from .. import models

register = template.Library()


@register.simple_tag
def get_invoice_item_component_amount(item, component):
    available_limits = plugins.manager.get_available_limits(component.component.offering.type)
    if item.limits and (
        component.component.billing_type == models.OfferingComponent.BillingTypes.USAGE or
        component.component.type in available_limits
    ):
        builtin_components = plugins.manager.get_components(component.component.offering.type)
        component_factors = {c.type: c.factor for c in builtin_components}
        factor = component_factors.get(component.component.type, 1)
        limit = item.limits.get(component.component.type, 0)
        return limit / factor

    return component.amount


@register.simple_tag
def plan_details(plan):
    context = {'plan': plan, 'components': []}

    for component in plan.components.all():
        offering_component = component.component

        if offering_component.billing_type == offering_component.BillingTypes.USAGE:
            continue

        context['components'].append({'name': offering_component.name,
                                      'amount': component.amount,
                                      'price': component.price})

    plan_template = template.Template(settings.WALDUR_MARKETPLACE.get('PLAN_TEMPLATE'))
    return plan_template.render(template.Context(context, autoescape=False))
