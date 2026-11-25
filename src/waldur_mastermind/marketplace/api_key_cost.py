"""What a resource API key's usage costs, for ranking keys across offerings.

Offerings meter different components (``token_cost`` in USD, input and output
tokens, storage), so raw usage cannot rank keys of different offerings against
each other. A key's estimated cost can: it is the sum over its
``current_usages`` of the converted quantity times the unit price of that
component in the resource's plan — the price and conversion ``billing_usage``
uses for invoice items, without discounts, credits or carried-over usage.

It is stored on the key (``ResourceApiKey.estimated_cost``) so a list can sort
on it, and recomputed whenever a usage report writes ``current_usages``. A plan
price change therefore shows at the key's next report. The figure covers
``usage_period``; ``current_estimated_cost`` reads it as zero once that month
is over and no usage has come in for the new one.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import NamedTuple

from django.db.models import Case, DecimalField, F, Sum, Value, When

from waldur_mastermind.marketplace import billing_mode, models
from waldur_mastermind.marketplace.billing_utils import convert_quantity


class UnitPrice(NamedTuple):
    """Unit price and resolved billing type for one priced component."""

    price: Decimal
    billing_type: str


UnitPrices = dict[str, UnitPrice]


def unit_prices(resource: models.Resource) -> UnitPrices:
    """What one unit of each priced component costs under the resource's plan,
    with what converting a usage into that unit needs. Components the plan does
    not price are left out, as is everything for a resource without a plan."""
    plan = resource.plan
    if plan is None:
        return {}
    components = {
        component.pk: component for component in resource.offering.components.all()
    }
    return {
        components[plan_component.component_id].type: UnitPrice(
            plan_component.price,
            billing_mode.resolve_component(
                components[plan_component.component_id], plan
            ).billing_type,
        )
        for plan_component in plan.components.all()
        if plan_component.price and plan_component.component_id in components
    }


def cost_of(
    resource: models.Resource,
    usages: Mapping[str, Decimal | int | float],
    prices: UnitPrices | None = None,
) -> Decimal:
    """What ``usages`` (``{component_type: amount}``) cost at the resource
    plan's prices. Pass ``prices`` from unit_prices to cost many usages of one
    resource without reading the plan again."""
    if not usages:
        return Decimal(0)
    if prices is None:
        prices = unit_prices(resource)
    total = Decimal(0)
    for component_type, usage in usages.items():
        if component_type not in prices:
            continue
        unit_price = prices[component_type]
        quantity = convert_quantity(
            usage,
            resource.offering.type,
            component_type,
            billing_type=unit_price.billing_type,
        )
        total += Decimal(str(quantity)) * unit_price.price
    return total


def estimate(api_key: models.ResourceApiKey) -> Decimal:
    """The cost of the key's ``current_usages`` at its resource plan's prices.

    A component the plan does not price, or one the offering no longer has,
    counts as 0, as does every component of a resource without a plan.
    """
    return cost_of(api_key.resource, api_key.current_usages or {})


def current_estimated_cost(period):
    """``estimated_cost`` as of the month ``period``, as a queryset expression.

    A key whose last report is for an earlier month has spent nothing in this
    one yet, so it ranks as zero rather than at last month's figure. So does a
    key whose offering no longer manages keys: the API shows its usage and cost
    as null, and a list sorted or totalled by cost must agree with that.
    """
    return Case(
        When(
            usage_period=period,
            resource__offering__plugin_options__enable_api_key_provisioning=True,
            then=F("estimated_cost"),
        ),
        default=Value(Decimal(0)),
        output_field=DecimalField(),
    )


def total(api_keys, period) -> Decimal:
    """The keys' estimated cost in the month ``period``, summed across offerings."""
    result = api_keys.order_by().aggregate(total=Sum(current_estimated_cost(period)))[
        "total"
    ]
    return result or Decimal(0)
