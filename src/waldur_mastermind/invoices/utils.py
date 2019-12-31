import base64
import datetime
from calendar import monthrange
from decimal import Decimal

import pdfkit
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.common.mixins import UnitPriceMixin


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


def get_current_month_days():
    now = timezone.now()
    range = monthrange(now.year, now.month)
    return range[1]


def get_full_hours(start, end):
    seconds_in_hour = 60 * 60
    full_hours, extra_seconds = divmod((end - start).total_seconds(), seconds_in_hour)
    if extra_seconds > 0:
        full_hours += 1

    return int(full_hours)


def check_past_date(year, month, day=None):
    day = day or 1

    try:
        return datetime.date(year=int(year), month=int(month), day=int(day)) <= timezone.now().date()
    except ValueError:
        return False


def parse_period(attrs, use_default=True):
    year = use_default and get_current_year() or None
    month = use_default and get_current_month() or None

    try:
        year = int(attrs.get('year', ''))
        month = int(attrs.get('month', ''))
    except ValueError:
        pass

    return year, month


def get_previous_month():
    date = timezone.now()
    month, year = (date.month - 1, date.year) if date.month != 1 else (12, date.year - 1)
    return datetime.date(year, month, 1)


def filter_invoice_items(items):
    return [item for item in items if item.total != 0]  # skip empty, but leave in credit and debit


def create_invoice_pdf(invoice):
    all_items = filter_invoice_items(invoice.items)
    logo_path = settings.WALDUR_CORE['SITE_LOGO']
    if logo_path:
        with open(logo_path, 'rb') as image_file:
            deployment_logo = base64.b64encode(image_file.read()).decode("utf-8")
    else:
        deployment_logo = None

    context = dict(
        invoice=invoice,
        issuer_details=settings.WALDUR_INVOICES['ISSUER_DETAILS'],
        currency=settings.WALDUR_CORE['CURRENCY_NAME'],
        deployment_logo=deployment_logo,
        items=all_items,
    )
    html = render_to_string('invoices/invoice.html', context)
    pdf = pdfkit.from_string(html, False)
    invoice.file = str(base64.b64encode(pdf), 'utf-8')
    invoice.save()


def get_price_per_day(price, unit):
    if unit == UnitPriceMixin.Units.PER_DAY:
        return price
    elif unit == UnitPriceMixin.Units.PER_MONTH:
        return price / Decimal(30)
    elif unit == UnitPriceMixin.Units.PER_HALF_MONTH:
        return price / Decimal(15)
    elif unit == UnitPriceMixin.Units.PER_HOUR:
        return price * 24
    else:
        return price
