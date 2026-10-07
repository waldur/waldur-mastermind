import datetime
import json
import logging
import re
import threading
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from pathlib import Path
from uuid import UUID

import typst
from constance import config
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import F, QuerySet, Sum
from django.db.models.functions.comparison import Coalesce
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from waldur_core.core import utils as core_utils
from waldur_core.structure.models import Customer

from . import models

logger = logging.getLogger(__name__)


def affiliates_feature_enabled() -> bool:
    """The affiliate program is opt-in via the AFFILIATES_ENABLED Constance
    setting, disabled by default.

    Gates both the customer-affiliates API and fee accrual at invoice
    finalization. Configured links are kept but stay dormant while the
    feature is off. The `reseller.affiliates` entry in core features only
    controls homeport element visibility and is not consulted here.
    """
    return config.AFFILIATES_ENABLED


def get_current_month():
    return timezone.now().month


def get_current_year():
    return timezone.now().year


def get_current_month_end():
    return core_utils.month_end(timezone.now())


def get_current_month_start():
    return core_utils.month_start(timezone.now())


def get_full_days(start, end):
    seconds_in_day = 24 * 60 * 60
    full_days, extra_seconds = divmod((end - start).total_seconds(), seconds_in_day)
    if extra_seconds > 0:
        full_days += 1

    return int(full_days)


def get_full_hours(start, end):
    seconds_in_hour = 60 * 60
    full_hours, extra_seconds = divmod((end - start).total_seconds(), seconds_in_hour)
    if extra_seconds > 0:
        full_hours += 1

    return int(full_hours)


def check_past_date(year, month, day=None):
    day = day or 1

    try:
        return (
            datetime.date(year=int(year), month=int(month), day=int(day))
            <= timezone.now().date()
        )
    except ValueError:
        return False


def parse_period(attrs, use_default=True):
    year = use_default and get_current_year() or None
    month = use_default and get_current_month() or None

    try:
        year = int(attrs.get("year", ""))
        month = int(attrs.get("month", ""))
    except ValueError:
        pass

    return year, month


def get_previous_month():
    date = timezone.now()
    month, year = (
        (date.month - 1, date.year) if date.month != 1 else (12, date.year - 1)
    )
    return datetime.date(year, month, 1)


def filter_invoice_items(
    items: QuerySet,
    query: str | None = None,
    provider_uuid: str | UUID | None = None,
    project_uuid: str | UUID | None = None,
    offering_uuid: str | UUID | None = None,
    conceal_compensation_items: bool = False,
    ordering: str | None = None,
) -> list:
    """
    Filter invoice items based on various criteria.

    Args:
        items: QuerySet or list of invoice items
        query: search filter
        provider_uuid: Filter by provider UUID
        project_uuid: Filter by project UUID
        offering_uuid: Filter by offering UUID
        conceal_compensation_items: If True, filter out credit compensation items

    Returns:
        Filtered list of invoice items
    """

    if query:
        # Currently frontend query filter is to filter by resource name
        items = items.filter(resource__name__icontains=query)

    if provider_uuid:
        items = items.filter(details__service_provider_uuid=provider_uuid)

    if project_uuid:
        items = items.filter(project_uuid=project_uuid)

    if offering_uuid:
        items = items.filter(details__offering_uuid=offering_uuid)

    if conceal_compensation_items:
        items = items.filter(credit__isnull=True)

    # Apply ordering in database
    if ordering:
        # Map ordering fields to database fields
        ordering_map = {
            "project_name": "project_name",
            "-project_name": "-project_name",
            "resource_name": "resource__name",
            "-resource_name": "-resource__name",
            "name": "name",  # InvoiceItem name field
            "-name": "-name",
            "provider_name": "resource__offering__customer__name",
            "-provider_name": "-resource__offering__customer__name",
        }

        db_ordering = ordering_map.get(ordering)
        if db_ordering:
            items = core_utils.order_with_nulls(items, db_ordering)

    result = [
        item for item in items if item.total != 0
    ]  # skip empty, but leave in credit and debit

    return result


INVOICE_PDF_DIR = Path(__file__).parent / "pdf"
CENT = Decimal("0.01")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _format_money(value: Decimal, currency) -> str:
    return f"{currency} {value:,.2f}"


def _format_decimal(value: Decimal, min_places=0) -> str:
    """Thousands-grouped, with every significant decimal and at least min_places.

    Quantities and unit prices are stored with PRICE_DECIMAL_PLACES, so a fixed
    precision either prints 30.0000000 or rounds a 0.012 per-hour rate to 0.01.
    """
    whole, _sep, fraction = f"{value:,f}".partition(".")
    fraction = fraction.rstrip("0").ljust(min_places, "0")
    return f"{whole}.{fraction}" if fraction else whole


def _format_unit_price(value: Decimal, currency) -> str:
    return f"{currency} {_format_decimal(value, min_places=2)}"


def _format_period(item) -> str:
    if not item.start or not item.end:
        return ""
    start = timezone.localtime(item.start).strftime("%Y-%m-%d %H:%M")
    end = timezone.localtime(item.end).strftime("%Y-%m-%d %H:%M")
    return f"{start} – {end}"


def format_percent(value) -> str:
    """Two decimals at most, without trailing zeros: 56.59094 -> 56.59, 50.0 -> 50."""
    rounded = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    return f"{rounded:f}".rstrip("0").rstrip(".")


def _discount_label(item) -> str:
    """Mirrors the label Homeport shows for a volume-discount row."""
    details = item.details or {}
    label = str(_("Volume discount"))
    component = details.get("offering_component_name") or details.get(
        "offering_component_type"
    )
    if component:
        label += f" — {component}"
    if details.get("discount_percent") is not None:
        label += f" ({format_percent(details['discount_percent'])}%)"
    return label


def _pdf_row(item, price, currency) -> dict:
    if (item.details or {}).get("is_discount"):
        # Quantity is always 1 and the period is the discounted item's own,
        # so only the amount is worth printing.
        return {
            "name": _discount_label(item),
            "period": "",
            "quantity": "",
            "unit": "",
            "unit_price": "",
            "price": _format_money(price, currency),
            "discount": True,
        }
    return {
        "name": item.name,
        "period": _format_period(item),
        "quantity": _format_decimal(item.quantity),
        "unit": str(item.get_measured_unit()),
        "unit_price": _format_unit_price(item.unit_price, currency),
        "price": _format_money(price, currency),
        "discount": False,
    }


def _join_present(*parts, separator=", ") -> str:
    return separator.join(str(part) for part in parts if part)


def _issuer_lines(issuer: dict) -> list[str]:
    phone = issuer.get("phone") or {}
    phone_line = _join_present(
        f"({phone['country_code']})" if phone.get("country_code") else "",
        phone.get("national_number"),
        separator=" ",
    )
    lines = [
        issuer.get("company") or "",
        issuer.get("address"),
        _join_present(issuer.get("country"), issuer.get("postal")),
        phone_line,
        _join_present(issuer.get("bank"), issuer.get("account")),
        f"{_('VAT')}: {issuer['vat_code']}" if issuer.get("vat_code") else "",
        issuer.get("email"),
    ]
    return [lines[0]] + [line for line in lines[1:] if line]


def _customer_lines(customer: Customer) -> list[str]:
    lines = [
        customer.name,
        customer.address,
        _join_present(customer.country, customer.postal),
        customer.phone_number,
        _join_present(customer.bank_name, customer.bank_account),
        f"{_('VAT')}: {customer.vat_code}" if customer.vat_code else "",
        customer.email,
    ]
    return [lines[0]] + [str(line) for line in lines[1:] if line]


def get_invoice_pdf_data(invoice: "models.Invoice") -> dict:
    """Everything the Typst invoice template prints, as preformatted strings.

    The printed figures must add up on paper. InvoiceItem.price is already
    rounded to the cent, so the subtotal is exactly the sum of the printed rows
    (and equals Invoice.price). Invoice.tax and Invoice.total are unrounded, so
    VAT is rounded once here and the total is the printed subtotal plus VAT.
    """
    currency = config.CURRENCY_NAME
    title = f"{_('Invoice No.')} {invoice.number}"

    facts = [
        {
            "label": str(_("Invoice date")),
            "value": invoice.invoice_date.isoformat()
            if invoice.invoice_date
            else str(_("Pending")),
        },
    ]
    if invoice.due_date:
        facts.append(
            {"label": str(_("Due date")), "value": invoice.due_date.isoformat()}
        )
    facts.append(
        {
            "label": str(_("Invoice period")),
            "value": f"{invoice.year}-{invoice.month:02d}",
        }
    )

    rows = [
        (item, item.price)
        # The unit falls back to the resource's scope type when none is stored
        for item in invoice.items.select_related(
            "project", "resource"
        ).prefetch_related("resource__scope")
        if item.price != 0
    ]
    # A volume discount is printed right under the item it reduces, which it
    # names in details["discount_of_item"]. One whose item is not printed
    # stays a row of its own.
    printed_uuids = {item.uuid.hex for item, _price in rows}
    discounts_by_item: dict[str, list[tuple]] = {}
    for item, price in rows:
        target = (item.details or {}).get("discount_of_item")
        if (item.details or {}).get("is_discount") and target in printed_uuids:
            discounts_by_item.setdefault(target, []).append((item, price))
    paired = {item.uuid for group in discounts_by_item.values() for item, _ in group}

    # Grouped by project identity, not name: two projects may share a name.
    projects: dict[tuple[str, str], list[tuple]] = {}
    for item, price in rows:
        if item.uuid in paired:
            continue
        key = (item.get_project_name(), str(item.get_project_uuid() or ""))
        projects.setdefault(key, []).append((item, price))

    subtotal = Decimal(0)
    project_rows = []
    for (name, _uuid), project_items in sorted(projects.items()):
        project_items.sort(
            key=lambda row: (row[0].name, row[0].start or timezone.now())
        )
        printed = []
        for item, price in project_items:
            printed.append((item, price))
            printed.extend(discounts_by_item.get(item.uuid.hex, []))
        subtotal += sum(price for _item, price in printed)
        project_rows.append(
            {
                "name": name,
                "items": [_pdf_row(item, price, currency) for item, price in printed],
            }
        )

    vat = _round_money(subtotal * invoice.tax_percent / 100)
    totals = [
        {
            "label": str(_("Subtotal")),
            "value": _format_money(subtotal, currency),
            "emphasis": False,
        }
    ]
    if vat:
        totals.append(
            {
                "label": str(_("VAT")),
                "value": _format_money(vat, currency),
                "emphasis": False,
            }
        )
    totals.append(
        {
            "label": str(_("TOTAL")),
            "value": _format_money(subtotal + vat, currency),
            "emphasis": True,
        }
    )

    return {
        "title": title,
        "labels": {
            "issuer": str(_("From")),
            "customer": str(_("Bill to")),
            "item": str(_("Item")),
            "quantity": str(_("Quantity")),
            "unit": str(_("Unit")),
            "unit_price": str(_("Unit price")),
            "price": str(_("Price")),
        },
        "facts": facts,
        "issuer": _issuer_lines(settings.WALDUR_INVOICES["ISSUER_DETAILS"]),
        "customer": _customer_lines(invoice.customer),
        "projects": project_rows,
        "totals": totals,
    }


_invoice_pdf_compiler = None
_invoice_pdf_compiler_lock = threading.Lock()


def create_invoice_pdf(invoice: "models.Invoice") -> bytes:
    """Render the invoice PDF.

    The compiler is built once per process: it scans the bundled fonts and the
    host fonts, which is far slower than compiling one invoice. Bundled Source
    Sans 3 comes first; host fonts are only a fallback for scripts it lacks
    (CJK, Arabic, Hebrew, ...). The template reads nothing outside
    INVOICE_PDF_DIR and imports no packages, so it never touches the network.
    """
    global _invoice_pdf_compiler
    data = json.dumps(get_invoice_pdf_data(invoice))
    with _invoice_pdf_compiler_lock:
        if _invoice_pdf_compiler is None:
            _invoice_pdf_compiler = typst.Compiler(
                str(INVOICE_PDF_DIR / "invoice.typ"),
                root=str(INVOICE_PDF_DIR),
                font_paths=[str(INVOICE_PDF_DIR / "fonts")],
            )
        return _invoice_pdf_compiler.compile(sys_inputs={"invoice": data})


def get_end_date_for_profile(profile):
    end = profile.attributes.get("end_date")
    if end:
        result = re.match(r"\d{4}-\d{2}-\d{2}", end)
        if result:
            end = result.group(0)
        else:
            logger.error(
                f"The field 'end_date' for profile {profile} is not correct. Value: {end}"
            )
            return
        try:
            return datetime.datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            logger.error(
                f"The field 'end_date' for profile {profile} is not correct. Value: {end}"
            )


def get_upcoming_ends_of_fixed_payment_profiles():
    today = datetime.date.today()
    upcoming_ends = []

    for profile in models.PaymentProfile.objects.filter(
        is_active=True, payment_type=models.PaymentType.FIXED_PRICE
    ):
        end = get_end_date_for_profile(profile)

        if end and (end - today).days in [60, 30, 14, 1]:
            upcoming_ends.append(profile)

    return upcoming_ends


def get_monthly_invoicing_reports_context():
    ids_fixed = []
    today = datetime.date.today()
    context = {
        "contracts": [],
        "invoices": [],
        "month": today.month,
        "year": today.year,
    }

    for profile in models.PaymentProfile.objects.filter(
        payment_type=models.PaymentType.FIXED_PRICE, is_active=True
    ).order_by("organization__abbreviation", "organization__name"):
        ids_fixed.append(profile.organization.id)
        name = profile.organization.abbreviation or profile.organization.name
        end = get_end_date_for_profile(profile)

        if end and (end - today).days < 60:
            alarm = True
        else:
            alarm = False

        payments_sum = profile.payment_set.aggregate(sum=Sum("sum"))["sum"]
        contract_sum = profile.attributes.get("contract_sum")

        context["contracts"].append(
            {
                "name": name,
                "end": end,
                "end_date_alarm": alarm,
                "till_end": end and (end - today).days,
                "profile": profile,
                "payments_sum": payments_sum,
                "contract_sum": contract_sum,
                "payments_alarm": contract_sum and payments_sum != contract_sum,
            }
        )

    context["invoices"] = (
        models.Invoice.objects.exclude(customer_id__in=ids_fixed)
        .filter(month=today.month, year=today.year)
        .order_by("customer__abbreviation", "customer__name")
    )

    return context


def get_monthly_invoicing_reports():
    context = get_monthly_invoicing_reports_context()
    return render_to_string("invoices/monthly_invoicing_reports.html", context)


def get_billing_price_estimate_for_resources(resources):
    invoice_items = models.InvoiceItem.objects.filter(
        resource__in=resources,
        invoice__year=get_current_year(),
        invoice__month=get_current_month(),
    ).select_related("invoice")
    result = {
        "total": Decimal(0.0),
        "current": Decimal(0.0),
        "tax": Decimal(0.0),
        "tax_current": Decimal(0.0),
    }
    for item in invoice_items:
        result["current"] += item.price
        result["tax"] += item.tax
        result["tax_current"] += item.tax_current
        result["total"] += item.total
    return {k: f"{v:f}" for k, v in result.items()}


def get_billing_price_estimate_for_provider(
    customer: Customer, provider_uuid: str
) -> dict[str, Decimal]:
    """
    Calculate billing price estimates for a customer and specific provider.

    Aggregates invoice items for the current month and year, calculating
    total amounts, current charges, and taxes.

    Args:
    customer (Customer): Customer instance for whom to calculate estimates.
    provider_uuid (str): Customer UUID of the service provider.

    Returns:
        dict: Price estimates containing:
            - total: Total amount including taxes
            - current: Current charges without taxes
            - tax: Total tax amount
            - tax_current: Current tax amount

    Raises:
        ValidationError: If input parameters are invalid, such as missing or
            incorrect UUIDs, or if the provider does not exist.
        ValueError: If the provider UUID is not a valid UUID format.
        Customer.DoesNotExist: If no provider exists with the given UUID.
        Exception: For any other unforeseen errors during processing.

    """
    from waldur_mastermind.billing.utils import get_current_expression

    if not customer or not provider_uuid:
        raise ValidationError(_("Customer and provider UUID are required."))

    try:
        provider_uuid = str(UUID(provider_uuid))
        Customer.objects.get(uuid=provider_uuid)
    except ValueError:
        raise ValidationError(_("Invalid provider UUID format"))
    except Customer.DoesNotExist:
        raise ValidationError(
            _("Provider with UUID %s does not exist." % provider_uuid)
        )

    try:
        aggregated_data = (
            models.InvoiceItem.objects.filter(
                resource__offering__customer__uuid=provider_uuid,
                invoice__customer=customer,
                invoice__year=get_current_year(),
                invoice__month=get_current_month(),
            )
            .annotate(
                current_quantity=get_current_expression(),
                tax_factor=F("invoice__tax_percent") / 100,
            )
            .aggregate(
                total=Coalesce(
                    Sum(F("quantity") * F("unit_price") * (1 + F("tax_factor"))),
                    Decimal("0.00"),
                ),
                current=Coalesce(
                    Sum(F("current_quantity") * F("unit_price")),
                    Decimal("0.00"),
                ),
                tax=Coalesce(
                    Sum(F("quantity") * F("unit_price") * F("tax_factor")),
                    Decimal("0.00"),
                ),
                tax_current=Coalesce(
                    Sum(F("current_quantity") * F("unit_price") * F("tax_factor")),
                    Decimal("0.00"),
                ),
            )
        )

        result = {
            key: "{:f}".format(
                Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_UP)
            )
            for key, value in aggregated_data.items()
        }

        logger.debug(
            "Calculated billing estimate for provider %s: %s", provider_uuid, result
        )

        return result

    except Exception as e:
        logger.error(
            "Failed to calculate billing estimate",
            exc_info=e,
            extra={
                "customer_uuid": customer.uuid,
                "provider_uuid": provider_uuid,
            },
        )
        raise ValidationError(_("Failed to calculate billing estimate."))
