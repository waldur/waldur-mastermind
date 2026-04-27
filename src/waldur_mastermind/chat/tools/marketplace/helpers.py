"""Shared helpers for marketplace discovery tools used by the AI Assistant.

All accessors here assume anonymous-capped visibility: tools built on these
helpers return publicly viewable offerings only, regardless of caller
identity. That invariant is the core safety guarantee of the marketplace
tool set.
"""

from constance import config
from django.contrib.auth.models import AnonymousUser

from waldur_mastermind.marketplace import models as marketplace_models


def is_public_marketplace_enabled() -> bool:
    """True when anonymous users may view marketplace offerings."""
    return bool(config.ANONYMOUS_USER_CAN_VIEW_OFFERINGS)


def public_offerings_queryset():
    """Anonymous-capped queryset of publicly viewable offerings.

    Delegates to the model-layer manager so the Constance flag and the
    state/shared gates remain in one place.
    """
    return marketplace_models.Offering.objects.all().filter_by_ordering_availability_for_user(
        AnonymousUser()
    )


def get_homeport_url() -> str:
    """Homeport base URL with trailing slash stripped."""
    return (config.HOMEPORT_URL or "").rstrip("/")


def offering_homeport_url(offering_uuid) -> str:
    """Public offering detail page URL in Homeport."""
    return f"{get_homeport_url()}/marketplace-public-offering/{offering_uuid}/"


def _starting_price(offering):
    """Cheapest non-archived plan's unit_price formatted with currency.

    Returns ``"100.50 EUR"``-style strings (currency from
    ``config.CURRENCY_NAME``, default ``"EUR"``) so the LLM doesn't
    have to splice the currency code in itself — embedding it removes
    the chance it gets dropped from the rendered table.

    ``str(Decimal)`` would otherwise preserve the source exponent —
    a price stored as ``Decimal('0E-10')`` (zero with high precision)
    renders as the literal string ``'0E-10'``. The ``.2f`` format both
    pins decimals and normalises the value to plain notation.
    """
    cheapest = (
        offering.plans.filter(archived=False)
        .order_by("unit_price")
        .values_list("unit_price", flat=True)
        .first()
    )
    if cheapest is None:
        return None
    return f"{cheapest:.2f} {config.CURRENCY_NAME or 'EUR'}"


def serialize_offering_minimal(offering) -> dict:
    """LLM-friendly compact offering dict for search and comparison results.

    Does NOT expose ``offering.type`` (plugin identifier like
    ``Marketplace.Slurm`` / ``OpenStack.Tenant``) or the offering's API
    ``url`` — both leak into LLM narration as noise.

    DOES expose ``homeport_url``: the offering tools render results as a
    markdown table with a final ``Action`` column whose cell is a markdown
    link, and ``MarkdownBlock`` styles last-column links as a CTA button.
    Per-tool ``usage_instructions`` are explicit that the URL belongs
    ONLY inside ``[Open](url)``-style links and never in prose.
    """
    return {
        "name": offering.name,
        "uuid": str(offering.uuid),
        "category_title": offering.category.title if offering.category_id else "",
        "customer_name": offering.customer.name if offering.customer_id else "",
        "description": (offering.description or "")[:500],
        "starting_price": _starting_price(offering),
        "homeport_url": offering_homeport_url(offering.uuid),
    }


def serialize_offering_detailed(offering) -> dict:
    """Detailed offering dict for get_offering tool (plans + components + attrs)."""
    base = serialize_offering_minimal(offering)
    base.update(
        {
            "full_description": offering.full_description or "",
            "attributes": offering.attributes or {},
            "tags": [t.name for t in offering.tags.all()],
            "plans": [
                {
                    "uuid": str(p.uuid),
                    "name": p.name,
                    "unit_price": str(p.unit_price),
                    "unit": p.unit,
                }
                for p in offering.plans.filter(archived=False)
            ],
            "components": [
                {
                    "type": c.type,
                    "name": c.name,
                    "measured_unit": c.measured_unit,
                    "billing_type": c.billing_type,
                }
                for c in offering.components.all()
            ],
        }
    )
    return base
