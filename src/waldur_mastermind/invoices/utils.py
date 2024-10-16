import base64
import datetime
import logging
import re
from calendar import monthrange
from decimal import Decimal

from constance import config
from django.conf import settings
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.common.mixins import UnitPriceMixin

from . import models

logger = logging.getLogger(__name__)


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


def filter_invoice_items(items):
    return [
        item for item in items if item.total != 0
    ]  # skip empty, but leave in credit and debit


def create_invoice_html(invoice):
    all_items = filter_invoice_items(invoice.items.all())
    logo_path = config.SITE_LOGO
    if logo_path:
        with open(logo_path, "rb") as image_file:
            deployment_logo = base64.b64encode(image_file.read()).decode("utf-8")
    else:
        deployment_logo = None

    context = dict(
        invoice=invoice,
        issuer_details=settings.WALDUR_INVOICES["ISSUER_DETAILS"],
        currency=config.CURRENCY_NAME,
        deployment_logo=deployment_logo,
        items=all_items,
    )
    return render_to_string("invoices/invoice.html", context)


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
    )
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
    return result


class MonthlyCompensation:
    def __init__(self, customer):
        self.customer = customer
        self.invoice = (
            models.Invoice.objects.filter(state=models.Invoice.States.PENDING)
            .order_by("-year", "-month")
            .first()
        )
        self.compensations = []
        self.projects_credits = []
        self.total_compensation = 0
        self.tail = 0
        self.credit = None

        if not self.invoice:
            return

        credit = models.CustomerCredit.objects.filter(
            customer=self.invoice.customer
        ).first()

        if not credit or not credit.value:
            return

        items_projects_ids = self.invoice.items.all().values_list(
            "project_id", flat=True
        )

        if not items_projects_ids:
            return

        projects_credits = {
            p.project: p
            for p in models.ProjectCredit.objects.filter(
                project_id__in=items_projects_ids
            )
        }

        items = sorted(
            [i for i in self.invoice.items.all() if i.resource],
            key=models.InvoiceItem._price,
        )

        for item in items:
            if (
                credit.offerings.all()
                and item.resource.offering not in credit.offerings.all()
            ):
                continue

            project_credit = projects_credits.get(item.project, None)
            cost = item.total

            if project_credit:
                if cost >= project_credit.value:
                    cost -= project_credit.value
                    credit_compensation = project_credit.value  # item compensation
                    project_credit.value = 0
                    credit.value -= credit_compensation

                    if project_credit.use_organisation_credit and cost:
                        if cost >= credit.value:
                            credit_compensation += credit.value
                            credit.value = 0
                        else:
                            credit_compensation += cost
                            credit.value -= cost
                else:
                    credit_compensation = cost
                    project_credit.value -= cost
                    credit.value -= cost

            else:
                if cost >= credit.value:
                    credit_compensation = credit.value
                    credit.value = 0
                else:
                    credit_compensation = cost
                    credit.value -= cost

            if credit_compensation:
                self.compensations.append(
                    models.InvoiceItem(
                        invoice=self.invoice,
                        unit_price=credit_compensation * -1,
                        quantity=1,
                        unit=models.InvoiceItem.Units.QUANTITY,
                        credit=credit,
                        name=f"Credit compensation. {item}",
                        resource=item.resource,
                    )
                )

            if not credit.value:
                break

        self.total_compensation = sum(
            credit.unit_price * -1 for credit in self.compensations
        )
        self.tail = 0

        if credit.minimal_consumption:
            if self.total_compensation < credit.minimal_consumption:
                self.tail = credit.minimal_consumption - self.total_compensation

                if credit.value - self.tail < 0:
                    self.tail = credit.value
                    credit.value = 0
                else:
                    credit.value -= self.tail

                self.total_compensation += self.tail

        self.projects_credits = projects_credits.values()
        self.credit = credit

    def save(self):
        if not self.credit:
            return

        models.InvoiceItem.objects.bulk_create(self.compensations)

        for pc in self.projects_credits:
            pc.save()

        self.credit.save()

    def get_project_credit_consumption(self, project):
        if [p for p in self.projects_credits if p.project == project]:
            projects_credit = [
                p for p in self.projects_credits if p.project == project
            ][0]
            new_project_value = projects_credit.value
            projects_credit.refresh_from_db()
            old_project_value = projects_credit.value
            return old_project_value - new_project_value

        return 0

    def get_project_compensation(self, project):
        return sum(
            [
                c.unit_price * -1
                for c in self.compensations
                if c.resource.project == project
            ]
        )
