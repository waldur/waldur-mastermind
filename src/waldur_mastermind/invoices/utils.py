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
from waldur_core.structure import models as structure_models
from waldur_mastermind.common.mixins import UnitPriceMixin

from . import log, models

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
            models.Invoice.objects.filter(
                state=models.Invoice.States.PENDING, customer=customer
            )
            .order_by("-year", "-month")
            .first()
        )
        self._calculated = False
        self._compensations = []
        self._projects_credits = []
        self._total_compensation = 0
        self._tail = 0

        self.credit = models.CustomerCredit.objects.filter(
            customer=self.customer
        ).first()

    def calculate_current_compensations(self):
        if self._calculated:
            return

        if not self.credit or not self.credit.value or not self.invoice:
            return

        items_projects_ids = self.invoice.items.all().values_list(
            "resource__project_id", flat=True
        )

        projects_credits = {
            p.project: p
            for p in models.ProjectCredit.objects.filter(
                project_id__in=items_projects_ids
            )
        }
        credit_offerings = list(self.credit.offerings.all())

        items = sorted(
            [
                i
                for i in self.invoice.items.exclude(resource__isnull=True)
                # if credit offerings are limited, check if item belongs to the limited offering
                if not credit_offerings or i.resource.offering in credit_offerings
            ],
            key=models.InvoiceItem._price,
        )

        for item in items:
            project_credit: models.ProjectCredit = projects_credits.get(
                item.project, None
            )
            cost = item.total

            if project_credit:
                if cost >= project_credit.value:
                    cost -= project_credit.value
                    credit_compensation = project_credit.value  # item compensation
                    project_credit.value = 0
                    self.credit.value -= credit_compensation
                else:
                    credit_compensation = cost
                    project_credit.value -= cost
                    self.credit.value -= cost

            else:
                if cost >= self.credit.value:
                    credit_compensation = self.credit.value
                    self.credit.value = 0
                else:
                    credit_compensation = cost
                    self.credit.value -= cost

            if credit_compensation:
                self._compensations.append(
                    models.InvoiceItem(
                        invoice=self.invoice,
                        unit_price=credit_compensation * -1,
                        quantity=1,
                        unit=models.InvoiceItem.Units.QUANTITY,
                        credit=self.credit,
                        name=f"Credit compensation. {item}",
                        resource=item.resource,
                        project=item.resource.project,
                    )
                )

            if not self.credit.value:
                break

        self._total_compensation = sum(
            credit.unit_price * -1 for credit in self._compensations
        )
        self._tail = 0

        if self.credit.minimal_consumption:
            if self._total_compensation < self.credit.minimal_consumption:
                self._tail = self.credit.minimal_consumption - self._total_compensation

                if self.credit.value - self._tail < 0:
                    self._tail = self.credit.value
                    self.credit.value = 0
                else:
                    self.credit.value -= self._tail

                self._total_compensation += self._tail

        self._projects_credits = projects_credits.values()
        self._calculated = True
        return

    @property
    def compensations(self):
        self.calculate_current_compensations()
        return self._compensations

    @property
    def projects_credits(self):
        self.calculate_current_compensations()
        return self._projects_credits

    @property
    def total_compensation(self):
        self.calculate_current_compensations()
        return self._total_compensation

    @property
    def tail(self):
        self.calculate_current_compensations()
        return self._tail

    @staticmethod
    def calculate_linear_minimal_consumption(
        customer: structure_models.Customer,
        credit_value: int | Decimal,
        end_date: datetime.date,
    ):
        today = datetime.date.today()
        months = (end_date.year * 12 + end_date.month) - (today.year * 12 + today.month)
        last_month = core_utils.get_last_month()

        if models.Invoice.objects.filter(
            customer=customer,
            year=last_month.year,
            month=last_month.month,
            state=models.Invoice.States.PENDING,
        ).exists():
            months += 1

        if not months:
            return credit_value

        return credit_value / months

    def update_linear_minimal_consumption(self):
        if (
            self.credit
            and self.credit.minimal_consumption_logic
            == models.CustomerCredit.MinimalConsumptionLogic.LINEAR
            and self.credit.end_date
        ):
            self.credit.minimal_consumption = (
                MonthlyCompensation.calculate_linear_minimal_consumption(
                    self.customer,
                    self.credit.value,
                    self.credit.end_date,
                )
            )
            self.credit.save(update_fields=["minimal_consumption"])

    def save(self):
        if not self.credit:
            return

        models.InvoiceItem.objects.bulk_create(self.compensations)

        for pc in self.projects_credits:
            pc.save()

        self.credit.save(update_fields=["value"])

    def get_project_credit_consumption(self, project):
        """Returns the value by which the project credit will be reduced next month."""

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
        """Returns the sum of compensation in the next month for the project."""

        return sum(
            [
                c.unit_price * -1
                for c in self.compensations
                if c.resource.project == project
            ]
        )

    def clear_compensations(self):
        """
        This method removes compensations in pended invoice.

        Attention!
        This method works correctly only if the minimal consumption has not changed since the moment
        compensation was applied for the current month and until now.
        Also this method does not work correctly if compensations have been applied
        but compensation items have not created and was consumption only due to minimal consumption.
        """

        if self._calculated:
            # If compensations have been calculated then we have dirty values of credits,
            # and we needed initiate the object again.
            self.__init__(self.customer)

        if not self.credit:
            return

        compensation_items = self.invoice.items.filter(credit=self.credit)

        if not compensation_items:
            return

        applied_compensations_sum = (
            compensation_items.aggregate(sum=Sum("unit_price"))["sum"] or 0
        ) * -1

        old_credit_value = self.credit.value
        self.credit.value += max(
            applied_compensations_sum, self.credit.minimal_consumption
        )
        self.credit.save()
        log.log_roll_back_customer_credit(
            self.credit.customer,
            old_credit_value,
            self.credit.value,
        )

        project_consumptions = list(
            compensation_items.values("project_id").annotate(value=Sum("unit_price"))
        )

        for project_credit in models.ProjectCredit.objects.filter(
            project__customer=self.customer
        ):
            value = [
                consumption["value"]
                for consumption in project_consumptions
                if consumption["project_id"] == project_credit.project.id
            ]

            if value:
                value = value[0] * -1
                old_project_credit_value = project_credit.value
                project_credit.value += value
                project_credit.save()
                log.log_roll_back_project_credit(
                    self.credit.customer,
                    project_credit.project,
                    old_project_credit_value,
                    project_credit.value,
                )

        compensation_items.delete()

    def apply_compensations(self):
        self.clear_compensations()
        self.save()
