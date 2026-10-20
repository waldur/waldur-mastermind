"""EU VAT rates and the rules that pick a rate for an invoice."""

import calendar
import datetime
import decimal
import logging

import requests
from django.conf import settings
from django.db import models as django_models
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from lxml import etree

from waldur_core.configuration import config
from waldur_core.structure import vies
from waldur_core.structure.models import Customer
from waldur_core.structure.vies import EU_COUNTRIES, EU_SERVICE_CODES, ISO_CODES

from . import models

logger = logging.getLogger(__name__)

TEDB_URL = "https://ec.europa.eu/taxation_customs/tedb/ws/VatRetrievalService"
TEDB_ACTION = (
    "urn:ec.europa.eu:taxud:tedb:services:v1:VatRetrievalService/RetrieveVatRates"
)
TYPES_NS = "urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService:types"

REQUEST_TEMPLATE = """<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:urn="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService"
    xmlns:urn1="{types_ns}">
  <soapenv:Header/>
  <soapenv:Body>
    <urn:retrieveVatRatesReqMsg>
      <urn1:memberStates>{codes}</urn1:memberStates>
      <urn1:situationOn>{date}</urn1:situationOn>
    </urn:retrieveVatRatesReqMsg>
  </soapenv:Body>
</soapenv:Envelope>"""


class VATRatesFetchError(Exception):
    pass


class VATError(Exception):
    """VAT cannot be calculated, so the invoice must not be finalized."""


class VATRule(django_models.TextChoices):
    OUTSIDE_EU = "outside_eu", _("Customer outside the EU")
    OPERATOR_COUNTRY = "operator_country", _("Same country as the operator")
    REVERSE_CHARGE = "reverse_charge", _("Verified EU VAT number (reverse charge)")
    CONSUMER_OSS = (
        "consumer_oss",
        _("EU customer without a verified VAT number: customer country rate (OSS)"),
    )
    CONSUMER = (
        "consumer",
        _("EU customer without a verified VAT number: operator country rate"),
    )


def parse_standard_rates(content: bytes) -> dict[str, decimal.Decimal]:
    """
    Return {iso_country: rate} from a TEDB response.

    Some countries list extra regional standard rates (e.g. Canary Islands),
    so the highest one per country is the mainland rate.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser=parser)
    ns = {"t": TYPES_NS}
    rates: dict[str, decimal.Decimal] = {}
    for result in root.iterfind(".//t:vatRateResults", ns):
        if result.findtext("t:type", namespaces=ns) != "STANDARD":
            continue
        code = result.findtext("t:memberState", namespaces=ns)
        value = result.findtext("t:rate/t:value", namespaces=ns)
        country = ISO_CODES.get(code, code)
        try:
            rate = decimal.Decimal(value)
        except (TypeError, decimal.InvalidOperation):
            rate = None
        if country not in EU_COUNTRIES or rate is None or not 0 <= rate < 100:
            logger.warning("Skipping unexpected VAT rate %r for %r.", value, code)
            continue
        if country not in rates or rate > rates[country]:
            rates[country] = rate
    return rates


def fetch_standard_rates(date: datetime.date):
    codes = "".join(
        f"<urn1:isoCode>{EU_SERVICE_CODES.get(c, c)}</urn1:isoCode>"
        for c in EU_COUNTRIES
    )
    body = REQUEST_TEMPLATE.format(
        types_ns=TYPES_NS, codes=codes, date=date.isoformat()
    )
    try:
        response = requests.post(
            TEDB_URL,
            data=body.encode(),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": TEDB_ACTION,
            },
            timeout=60,
        )
        response.raise_for_status()
        rates = parse_standard_rates(response.content)
    except (requests.RequestException, etree.XMLSyntaxError) as e:
        raise VATRatesFetchError(f"Unable to fetch EU VAT rates: {e}") from e
    if not rates:
        raise VATRatesFetchError("EU VAT rates response contained no standard rates.")
    return rates


def current_period() -> tuple[int, int]:
    today = timezone.localdate()
    return today.year, today.month


def period_end(year: int, month: int) -> datetime.date:
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def refresh_vat_rates(year: int | None = None, month: int | None = None):
    """Store the rates in force at the end of a billing month.

    A month that is not over yet gets today's rates.
    """
    if year is None or month is None:
        year, month = current_period()
    rates = fetch_standard_rates(min(timezone.localdate(), period_end(year, month)))
    with transaction.atomic():
        for country, rate in rates.items():
            models.VATRate.objects.update_or_create(
                country=country, year=year, month=month, defaults={"rate": rate}
            )
    logger.info(
        "Refreshed EU VAT rates of %s-%02d for %s countries.", year, month, len(rates)
    )
    return rates


def ensure_vat_rates(year: int, month: int):
    """Fetch a month's rates unless the stored ones are final.

    Rates of a past month are final once fetched after it ended; those of the
    current month once fetched at all. Never raises: invoicing goes on with
    the latest stored rates, and an invoice without any rate fails on its own.
    """
    end = period_end(year, month)
    stored = models.VATRate.objects.filter(year=year, month=month)
    if end < timezone.localdate():
        after_end = timezone.make_aware(
            datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time())
        )
        stored = stored.filter(fetched_at__gte=after_end)
    if stored.exists():
        return
    try:
        refresh_vat_rates(year, month)
    except Exception:
        logger.exception(
            "Unable to refresh EU VAT rates of %s-%02d, using stored ones.", year, month
        )


def get_operator_country() -> str:
    issuer = settings.WALDUR_INVOICES.get("ISSUER_DETAILS") or {}
    country = issuer.get("country_code") or settings.WALDUR_CORE.get(
        "SELLER_COUNTRY_CODE"
    )
    country = (country or "").upper()
    if country not in EU_COUNTRIES:
        raise VATError(
            f"Operator country {country!r} is not an EU member state. "
            "Set ISSUER_DETAILS['country_code'] to calculate EU VAT."
        )
    return country


def get_standard_rate(country: str, rates: dict) -> decimal.Decimal:
    rate = rates.get(country)
    if rate is None:
        raise VATError(f"No VAT rate stored for {country}.")
    return rate


def get_rates(year: int | None = None, month: int | None = None) -> dict:
    """{country: rate} of a billing month, the current one by default.

    A country missing that month falls back to its latest earlier month,
    so a failed fetch keeps invoicing on the last known rates.
    """
    if year is None or month is None:
        year, month = current_period()
    rows = (
        models.VATRate.objects.filter(Q(year__lt=year) | Q(year=year, month__lte=month))
        .order_by("country", "-year", "-month")
        .values_list("country", "rate")
    )
    rates: dict[str, decimal.Decimal] = {}
    for country, rate in rows:
        rates.setdefault(country, rate)
    return rates


def explain_vat(
    customer: Customer, rates: dict | None = None
) -> tuple[decimal.Decimal, VATRule]:
    """Return the VAT percent and the rule that picked it. Raises VATError.

    `rates` (from get_rates) picks the billing month; the current one by default.
    """
    if rates is None:
        rates = get_rates()
    operator_country = get_operator_country()
    country = (customer.country or "").upper() or operator_country

    if country not in EU_COUNTRIES:
        return decimal.Decimal(0), VATRule.OUTSIDE_EU

    if country == operator_country:
        return get_standard_rate(operator_country, rates), VATRule.OPERATOR_COUNTRY

    if not customer.is_private_person and customer.is_vat_code_verified:
        return decimal.Decimal(0), VATRule.REVERSE_CHARGE

    # Without a verified VAT number a company is taxed as a private person
    # (Implementing Regulation 282/2011, Art. 18(2)).
    if config.VAT_OSS_ENABLED:
        return get_standard_rate(country, rates), VATRule.CONSUMER_OSS
    return get_standard_rate(operator_country, rates), VATRule.CONSUMER


def get_vat_percent(
    customer: Customer, year: int | None = None, month: int | None = None
) -> decimal.Decimal:
    """Raises VATError when the rate cannot be determined."""
    return explain_vat(customer, get_rates(year, month))[0]


def needs_vat_code_check(customer: Customer) -> bool:
    """Only a foreign EU legal person's VAT number changes the rate."""
    country = (customer.country or "").upper()
    return (
        not customer.is_private_person
        and country in EU_COUNTRIES
        and country != get_operator_country()
        and vies.is_eu_vat_code(customer.vat_code, country)
    )


def recheck_vat_code(customer: Customer):
    """Confirm the VAT number is still registered. Keeps the last result if VIES is down."""
    if not needs_vat_code_check(customer):
        return
    try:
        result = vies.check_vat_number(customer.vat_code, customer.country)
    except vies.VIESUnavailable:
        logger.warning(
            "VIES is unavailable, keeping last check of %s for %s.",
            customer.vat_code,
            customer,
        )
        return
    if result.valid:
        customer.vat_verified_code = vies.normalize_vat_code(customer.vat_code)
        customer.vat_verified_at = timezone.now()
    else:
        logger.warning(
            "VAT number %s of %s is no longer valid.", customer.vat_code, customer
        )
        customer.vat_verified_code = ""
        customer.vat_verified_at = None
    customer.save(update_fields=["vat_verified_code", "vat_verified_at"])


def is_enabled() -> bool:
    return config.VAT_TAXATION_ENABLED


def apply_vat(invoice: models.Invoice) -> bool:
    """Set the invoice's VAT from the rules and its month's rates, unsaved.

    Returns True if anything changed. Raises VATError.
    """
    percent, rule = explain_vat(
        invoice.customer, get_rates(invoice.year, invoice.month)
    )
    # A company outside the EU is also taxed where it is established
    # (VAT Directive Art. 44), so its invoice carries the same statement.
    reverse_charge = rule == VATRule.REVERSE_CHARGE or (
        rule == VATRule.OUTSIDE_EU and not invoice.customer.is_private_person
    )
    if (invoice.tax_percent, invoice.vat_reverse_charge) == (percent, reverse_charge):
        return False
    invoice.tax_percent = percent
    invoice.vat_reverse_charge = reverse_charge
    return True


def update_invoice_tax_percent(invoice: models.Invoice) -> bool:
    """Store the VAT of the invoice's month on it. Returns True if it changed."""
    if not apply_vat(invoice):
        return False
    invoice.save(update_fields=["tax_percent", "vat_reverse_charge"])
    invoice.update_cache()
    return True


def prepare_finalization(invoices):
    """Fetch the rates and re-check the VAT numbers that finalizing needs.

    Runs before the per-invoice transactions so that no transaction is held
    open while waiting for TEDB or VIES. Never raises.
    """
    periods = set(invoices.values_list("year", "month").distinct())
    periods.add(current_period())
    for year, month in sorted(periods):
        ensure_vat_rates(year, month)
    for customer in Customer.objects.filter(
        pk__in=invoices.values("customer_id")
    ).iterator():
        try:
            recheck_vat_code(customer)
        except Exception:
            logger.exception("Unable to re-check the VAT number of %s.", customer)
