import datetime
import decimal
import logging
from calendar import monthrange
from typing import cast

from dateutil.parser import parse as parse_datetime
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import DatabaseError, models
from django.db.models import Index
from django.db.models.aggregates import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils import FieldTracker
from model_utils.tracker import FieldInstanceTracker
from rest_framework import exceptions as rf_exceptions
from reversion import revisions as reversion

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import get_connected_projects
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.enums import Units
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices.structures import InvoiceDetailsDict
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods

from . import utils

logger = logging.getLogger(__name__)


def get_created_date():
    now = timezone.now()
    return datetime.date(now.year, now.month, 1)


class Invoice(
    structure_models.StructureLoggableMixin,
    core_models.UuidMixin,
    core_models.BackendMixin,
    models.Model,
):
    """Invoice describes billing information about purchased resources for customers on a monthly basis"""

    items: models.Manager["InvoiceItem"]

    class Permissions:
        customer_path = "customer"

    class Meta:
        unique_together = ("customer", "month", "year")
        indexes = [
            Index(
                fields=["year", "month", "customer"], name="inv_invoice_year_month_idx"
            ),
        ]

    class States:
        PENDING = "pending"
        PENDING_FINALIZATION = "pending_finalization"
        CREATED = "created"
        PAID = "paid"
        CANCELED = "canceled"

        CHOICES = (
            (PENDING, _("Pending")),
            (PENDING_FINALIZATION, _("Pending finalization")),
            (CREATED, _("Created")),
            (PAID, _("Paid")),
            (CANCELED, _("Canceled")),
        )

        # Invoice states that still accept modifications (usage, items, etc.)
        MUTABLE_STATES = (PENDING, PENDING_FINALIZATION)

    month = models.PositiveSmallIntegerField(
        default=utils.get_current_month,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    year = models.PositiveSmallIntegerField(default=utils.get_current_year)
    created = models.DateField(null=True, blank=True, default=get_created_date)
    state = models.CharField(
        max_length=30, choices=States.CHOICES, default=States.PENDING
    )
    customer = models.ForeignKey(
        structure_models.Customer,
        verbose_name=_("organization"),
        related_name="+",
        on_delete=models.CASCADE,
    )
    total_cost = models.DecimalField(
        default=0,
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=2,
        help_text=_("Cached value for total cost."),
        editable=False,
    )
    total_price = models.DecimalField(
        default=0,
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=2,
        help_text=_("Cached value for total price."),
        editable=False,
    )
    tax_percent = models.DecimalField(
        default=0,
        max_digits=4,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    invoice_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Date then invoice moved from state pending to created."),
    )
    payment_url = models.URLField(
        help_text=_("URL for initiating payment via payment gateway."),
        blank=True,
    )
    reference_number = models.CharField(
        help_text=_("Reference number associated with the invoice."),
        max_length=300,
        blank=True,
    )

    def get_log_fields(self):
        return ("uuid", "name", "year", "month", "customer")

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def update_cache(self):
        """Update cached total_cost and total_price fields if they have changed."""
        updates = {}

        current_total = self.total
        # Convert to Decimal if not already and compare numeric values
        stored_total = (
            decimal.Decimal(self.total_cost)
            if not isinstance(self.total_cost, decimal.Decimal)
            else self.total_cost
        )
        if stored_total != current_total:
            updates["total_cost"] = current_total

        current_price = self.price
        # Convert to Decimal if not already and compare numeric values
        stored_price = (
            decimal.Decimal(self.total_price)
            if not isinstance(self.total_price, decimal.Decimal)
            else self.total_price
        )
        if stored_price != current_price:
            updates["total_price"] = current_price

        if updates:
            for field, value in updates.items():
                setattr(self, field, value)
            try:
                self.save(update_fields=list(updates.keys()))
            except DatabaseError as e:
                # Check if this is a race condition where invoice was deleted
                if "did not affect any rows" in str(e):
                    logger.debug(
                        "Invoice %s was deleted during cache update, skipping", self.pk
                    )
                else:
                    logger.error(
                        "Failed to update cached fields for Invoice %s: %s", self.pk, e
                    )

    @property
    def tax(self) -> decimal.Decimal:
        return self.price * self.tax_percent / 100

    @property
    def total(self) -> decimal.Decimal:
        return self.price + self.tax

    @property
    def price(self) -> decimal.Decimal:
        return quantize_price(
            decimal.Decimal(sum(item.price for item in self.items.all()))
        )

    @property
    def tax_current(self) -> decimal.Decimal:
        return self.price_current * self.tax_percent / 100

    @property
    def total_current(self) -> decimal.Decimal:
        return self.price_current + self.tax_current

    @property
    def price_current(self) -> decimal.Decimal:
        return sum(item.price_current for item in self.items.all())

    @property
    def due_date(self) -> datetime.date:
        if self.invoice_date:
            return self.invoice_date + datetime.timedelta(
                days=settings.WALDUR_INVOICES["PAYMENT_INTERVAL"]
            )

    @property
    def number(self) -> int:
        return 100000 + self.id

    def set_pending_finalization(self):
        """
        Change state from pending to pending_finalization.
        Used during grace period: invoice still accepts usage updates
        but is no longer the active month's invoice.
        """
        if self.state != self.States.PENDING:
            raise IncorrectStateException(_("Invoice must be in pending state."))
        self.state = self.States.PENDING_FINALIZATION
        self.save(update_fields=["state"])

    def set_created(self):
        """
        Change state from pending or pending_finalization to created or paid.
        """
        if self.state not in self.States.MUTABLE_STATES:
            raise IncorrectStateException(
                _("Invoice must be in pending or pending finalization state.")
            )

        if self.customer.paymentprofile_set.filter(
            is_active=True, payment_type=PaymentType.FIXED_PRICE
        ).count():
            self.state = self.States.PAID
        else:
            self.state = self.States.CREATED

        self.invoice_date = timezone.now().date()
        self.save(update_fields=["state", "invoice_date"])

    def __str__(self):
        return f"{self.customer} | {self.year}-{self.month}"


def get_quantity(unit, start, end) -> decimal.Decimal:
    """
    For fixed components this method computes number of billing periods resource
    was used from the time it was purchased or from the start of current month
    till the time it was terminated or billing plan has been switched or end of current month.
    """
    month_days = monthrange(start.year, start.month)[1]

    if unit == Units.PER_HOUR:
        return utils.get_full_hours(start, end)
    elif unit == Units.PER_DAY:
        return utils.get_full_days(start, end)
    elif unit == Units.PER_QUARTER:
        return core_utils.get_full_quarters(start, end)
    elif unit == Units.PER_HALF_MONTH:
        if (start.day == 1 and end.day == 15) or (
            start.day == 16 and end.day == month_days
        ):
            return 1
        elif start.day == 1 and end.day == month_days:
            return 2
        elif start.day == 1 and end.day > 15:
            return quantize_price(1 + (end.day - 15) / decimal.Decimal(month_days / 2))
        elif start.day < 16 and end.day == month_days:
            return quantize_price(
                1 + (16 - start.day) / decimal.Decimal(month_days / 2)
            )
        else:
            return quantize_price(
                (end.day - start.day + 1) / decimal.Decimal(month_days / 2.0)
            )
    # By default PER_MONTH
    else:
        if start.day == 1 and end.day == month_days:
            return 1

        use_days = (end - start).days + 1
        return quantize_price(decimal.Decimal(use_days) / month_days)


def filter_project_invoice_items(user):
    """Project-scope roles see invoice items of their projects as long as
    the customer displays billing info in projects."""
    return models.Q(
        project__in=get_connected_projects(user),
        invoice__customer__display_billing_info_in_projects=True,
    )


class InvoiceItem(
    structure_models.StructureLoggableMixin,
    core_models.UuidMixin,
    common_mixins.ProductCodeMixin,
    common_mixins.UnitPriceMixin,
):
    """
    It is expected that get_scope_type method is defined as class method in scope class
    as it is used in generic invoice item serializer.

    1) For fixed components quantity field stores number of days or hours resource
    was used from the time it was purchased or from the start of current month
    till the time it was terminated or billing plan has been switched or end of current month.

    2) For usage-based components quantity field stores amount of quota reported for the resource
    during the current billing period (ie month).

    3) For limit-based components quantity field stores amount of quota requested
    for the resource during provisioning. If limit type is monthly, this value is copied from
    previous billing period until resource is terminated.
    """

    class Permissions:
        customer_path = "invoice__customer"
        build_query = filter_project_invoice_items

    invoice = models.ForeignKey(
        on_delete=models.CASCADE, to=Invoice, related_name="items"
    )
    quantity = models.DecimalField(
        default=0,
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
    )
    measured_unit = models.CharField(
        max_length=30, help_text=_("Unit of measurement, for example, GB."), blank=True
    )
    resource = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=marketplace_models.Resource,
        related_name="invoice_items",
        null=True,
    )
    plan_component = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=marketplace_models.PlanComponent,
        related_name="invoice_items",
        null=True,
        blank=True,
    )
    name = models.TextField(default="")
    details: "InvoiceDetailsDict" = models.JSONField(
        default=dict, blank=True, help_text=_("Stores data about scope")
    )  # type: ignore

    start = models.DateTimeField(
        default=utils.get_current_month_start,
        help_text=_("Date and time when item usage has started."),
    )
    end = models.DateTimeField(
        default=utils.get_current_month_end,
        help_text=_("Date and time when item usage has ended."),
    )

    # Project name and UUID should be stored separately because project is not available after removal
    project = models.ForeignKey(
        structure_models.Project, on_delete=models.SET_NULL, null=True
    )
    project_name = models.CharField(
        max_length=structure_models.PROJECT_NAME_LENGTH, blank=True
    )
    project_uuid = models.CharField(max_length=32, blank=True)
    backend_uuid = models.UUIDField(null=True, blank=True)
    credit = models.ForeignKey["CustomerCredit"](
        "CustomerCredit", on_delete=models.SET_NULL, null=True, editable=False
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    @property
    def tax(self) -> decimal.Decimal:
        return self.price * self.invoice.tax_percent / 100

    @property
    def tax_current(self) -> decimal.Decimal:
        return self.price_current * self.invoice.tax_percent / 100

    @property
    def total(self) -> decimal.Decimal:
        return self.price + self.tax

    def _price(self, current=False) -> decimal.Decimal:
        """
        For components billed daily and hourly this method returns estimated price if `current` is True.
        Otherwise, it returns total price calculated using `quantity` field.
        It is assumed that value of `quantity` field is updated automatically when invoice item is terminated.
        """
        quantity = self.quantity
        if current:
            if self.unit == self.Units.PER_HOUR:
                quantity = utils.get_full_hours(
                    self.start, min(self.end, timezone.now())
                )
            if self.unit == self.Units.PER_DAY:
                quantity = utils.get_full_days(
                    self.start, min(self.end, timezone.now())
                )

        return quantize_price(self.unit_price * decimal.Decimal(quantity))

    def get_measured_unit(self) -> str:
        if self.measured_unit:
            return self.measured_unit

        if self.credit:
            return ""

        plural = self.quantity > 1

        if self.unit == self.Units.QUANTITY:
            if not self.resource or not self.resource.scope:
                return ""

            if getattr(self.resource.scope, "content_type", None):
                meta = self.resource.scope.content_type.model_class()._meta
            else:
                meta = self.resource.scope._meta
            return (
                str(meta.verbose_name_plural).lower()
                if plural
                else str(meta.verbose_name).lower()
            )
        elif self.unit == self.Units.PER_HOUR:
            return _("hours") if plural else _("hour")
        elif self.unit == self.Units.PER_DAY:
            return _("days") if plural else _("day")
        elif self.unit == self.Units.PER_HALF_MONTH:
            return _("percents from half a month")
        else:
            return _("percents from a month")

    def get_project_uuid(self) -> str | None:
        if self.project_uuid:
            return self.project_uuid
        if self.project:
            return self.project.uuid
        return None

    def get_project_name(self) -> str:
        if self.project_name:
            return self.project_name
        if self.project:
            return self.project.name
        return "N/A"

    @property
    def price(self) -> decimal.Decimal:
        return self._price()

    @property
    def price_current(self) -> decimal.Decimal:
        return self._price(current=True)

    def get_plan_component(self) -> marketplace_models.PlanComponent | None:
        # Use direct relationship first
        if self.plan_component:
            return self.plan_component

        # Fallback to details field for backward compatibility
        plan_component_id = self.details.get("plan_component_id")
        if not plan_component_id:
            return None
        try:
            return marketplace_models.PlanComponent.objects.get(id=plan_component_id)
        except marketplace_models.PlanComponent.DoesNotExist:
            return None

    def update_quantity(self):
        """
        For fixed-price component quantity is updated when item is terminated.
        For usage-based component quantity is updated when usage is reported.
        For limit-based component quantity is updated when limit is updated for total limit component
        or item is terminated for month or annual limit component.
        """
        plan_component = self.get_plan_component()
        if not plan_component:
            return
        if not plan_component.component:
            return
        if plan_component.component.billing_type == BillingTypes.FIXED or (
            plan_component.component.billing_type == BillingTypes.LIMIT
            and plan_component.component.limit_period != LimitPeriods.TOTAL
        ):
            self._update_quantity()

    def _update_quantity(self):
        new_quantity = get_quantity(self.unit, self.start, self.end)
        if new_quantity != self.quantity:
            self.quantity = new_quantity
        self.save(update_fields=["quantity"])

    def terminate(self, end=None):
        self.end = end or timezone.now()
        self.save(update_fields=["end"])
        self.update_quantity()

        resource_limit_periods = self.details.get("resource_limit_periods")
        if resource_limit_periods:
            # TOTAL limit period is a one-time charge — no day-based proration
            if self.details.get("limit_period") == "total":
                return

            last_period = resource_limit_periods[-1]
            last_period["end"] = self.end.isoformat()
            last_period["billing_periods"] = utils.get_full_days(
                parse_datetime(last_period["start"]), self.end
            )
            last_period["total"] = str(
                int(last_period["quantity"]) * last_period["billing_periods"]
            )
            self.quantity = sum(
                int(period["total"]) for period in resource_limit_periods
            )
            self.save(update_fields=["details", "quantity"])

    class Meta:
        indexes = [
            Index(fields=["resource", "invoice"], name="inv_item_resource_invoice_idx"),
        ]

    def __str__(self):
        return self.name or "<InvoiceItem %s>" % self.pk

    def get_log_fields(self):
        return ("uuid", "invoice")


class PaymentType(models.CharField):
    FIXED_PRICE = "fixed_price"
    MONTHLY_INVOICES = "invoices"
    PAYMENT_GW_MONTHLY = "payment_gw_monthly"

    CHOICES = (
        (FIXED_PRICE, "Fixed-price contract"),
        (MONTHLY_INVOICES, "Monthly invoices"),
        (PAYMENT_GW_MONTHLY, "Payment gateways (monthly)"),
    )

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 30
        kwargs["choices"] = self.CHOICES
        super().__init__(*args, **kwargs)


class PaymentProfile(core_models.UuidMixin, core_models.NameMixin, models.Model):
    organization = models.ForeignKey(
        structure_models.Customer, on_delete=models.PROTECT
    )
    payment_type = PaymentType()
    attributes = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(null=True, default=True)

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"{self.organization.name} ({self.payment_type})"

    class Permissions:
        customer_path = "organization"

    @classmethod
    def get_url_name(cls):
        return "payment-profile"

    def save(self, *args, **kwargs):
        if self.is_active is False:
            self.is_active = None

        if not self.tracker.previous(self.is_active) and self.is_active:
            self.__class__.objects.filter(organization=self.organization).exclude(
                pk=self.pk
            ).update(is_active=None)

        return super().save(*args, **kwargs)

    class Meta:
        unique_together = ("organization", "is_active")


class Payment(core_models.UuidMixin, core_models.TimeStampedModel):
    profile = models.ForeignKey(
        PaymentProfile, on_delete=models.PROTECT, null=False, blank=False
    )
    sum = models.DecimalField(
        default=0, max_digits=10, decimal_places=2, null=False, blank=False
    )
    date_of_payment = models.DateField(
        null=False,
        blank=False,
    )
    proof = models.FileField(upload_to="proof_of_payment", null=True, blank=True)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Permissions:
        customer_path = "profile__organization"

    @classmethod
    def get_url_name(cls):
        return "payment"


class BaseCredit(core_models.UuidMixin, core_models.TimeStampedModel):
    class MinimalConsumptionLogic:
        FIXED = "fixed"
        LINEAR = "linear"

        CHOICES = (
            (FIXED, "Fixed"),
            (LINEAR, "Linear"),
        )

    expected_consumption = models.DecimalField(
        default=0,
        validators=[MinValueValidator(decimal.Decimal("0"))],
        max_digits=16,
        decimal_places=5,
    )
    minimal_consumption_logic = models.CharField(
        max_length=10,
        choices=MinimalConsumptionLogic.CHOICES,
        default=MinimalConsumptionLogic.FIXED,
    )
    grace_coefficient = models.DecimalField(
        max_digits=3,
        decimal_places=0,
        default=decimal.Decimal("0"),
    )
    apply_as_minimal_consumption = models.BooleanField(default=True)
    end_date = models.DateField(null=True)
    value = models.DecimalField(
        default=0,
        validators=[MinValueValidator(decimal.Decimal("0"))],
        max_digits=16,
        decimal_places=5,
    )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        end_date_being_written = update_fields is None or "end_date" in update_fields
        if end_date_being_written and self.end_date and self.end_date.day != 1:
            raise rf_exceptions.ValidationError(
                {"end_date": "End date must be the first day of the month."}
            )
        super().save(*args, **kwargs)

    @property
    def time_left_factor(self) -> decimal.Decimal:
        today = datetime.date.today()
        days_until_credit_end = decimal.Decimal(
            (self.end_date.replace(day=1) - today).days
        )

        if days_until_credit_end <= 0:
            return decimal.Decimal("1")

        days_in_current_month = decimal.Decimal(monthrange(today.year, today.month)[1])
        return min(decimal.Decimal("1"), days_in_current_month / days_until_credit_end)

    def calculate_linear_expected_consumption(
        self, total_compensation
    ) -> decimal.Decimal:
        return (
            max(decimal.Decimal("0"), self.expected_consumption - total_compensation)
            * (decimal.Decimal("1") - self.time_left_factor)
            + self.value * self.time_left_factor
        )

    @property
    def minimal_consumption(self) -> decimal.Decimal:
        if not self.apply_as_minimal_consumption:
            return 0

        if (
            self.end_date
            and self.end_date.year == datetime.date.today().year
            and self.end_date.month == datetime.date.today().month
        ):
            return self.expected_consumption

        return (100 - self.grace_coefficient) / 100 * self.expected_consumption

    class Meta:
        abstract = True


class CustomerCredit(BaseCredit):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)
    offerings = models.ManyToManyField(marketplace_models.Offering)

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "customer"

    @property
    def allocated_to_projects(self) -> float:
        return (
            ProjectCredit.objects.filter(project__customer=self.customer).aggregate(
                sum=Sum("value")
            )["sum"]
            or 0
        )

    @property
    def consumption_last_month(self) -> float:
        last_month = core_utils.get_last_month()
        invoice = Invoice.objects.filter(
            year=last_month.year,
            month=last_month.month,
            customer=self.customer,
        ).first()

        if not invoice:
            return

        items = InvoiceItem.objects.filter(
            invoice=invoice,
            credit=self,
        )
        consumption = sum([i.total for i in items]) or 0

        return consumption * -1

    @property
    def withdrawable_balance(self) -> decimal.Decimal:
        """Part of the credit that may leave the platform via payouts or
        transfers: earnings-typed ledger inflows minus outflows, capped by
        the current credit value so that staff-granted (promotional) credit
        is never withdrawable and credit expiry wipes earnings too.
        """
        # Prefer the queryset annotation (list endpoint) when present, else
        # aggregate on demand (detail view, direct model access).
        earned = getattr(self, "withdrawable_earned_agg", None)
        if earned is None:
            earned = self.transactions.filter(
                transaction_type__in=CreditTransaction.Types.WITHDRAWABLE_TYPES
            ).aggregate(sum=Sum("amount"))["sum"]
        earned = earned or 0
        return max(decimal.Decimal("0"), min(earned, self.value))

    def __str__(self):
        return f"Customer credit for {self.customer.name}, value {self.value}"


class ProjectCredit(BaseCredit):
    project = models.OneToOneField(structure_models.Project, on_delete=models.CASCADE)
    mark_unused_credit_as_spent_on_project_termination = models.BooleanField(
        default=False
    )

    @property
    def consumption_last_month(self) -> float | None:
        """Credit drawn by this project in the previous month.

        None when that month has no invoice at all — no billing period is not
        the same statement as "drew nothing", and callers should be able to
        tell them apart.
        """
        last_month = core_utils.get_last_month()
        invoice = Invoice.objects.filter(
            year=last_month.year,
            month=last_month.month,
            customer=self.project.customer,
        ).first()

        if not invoice:
            return None

        # Nothing links a ProjectCredit to its CustomerCredit — no FK, no
        # delete guard — so the organization credit can be removed while
        # project credits survive. Without it no compensation item can exist,
        # so nothing was drawn; raising here would 500 every read of the
        # project credit, which project roles now make.
        credit = CustomerCredit.objects.filter(customer=self.project.customer).first()
        if not credit:
            return 0

        items = InvoiceItem.objects.filter(
            invoice=invoice,
            credit=credit,
            project=self.project,
        )
        consumption = sum([i.total for i in items]) or 0
        return consumption * -1

    @property
    def spendable_value(self) -> decimal.Decimal:
        """Credit this project can actually draw this month.

        `value` is only an allocation: compensation stops as soon as the
        organization credit is exhausted (see MonthlyCompensation), so a
        project can show a healthy balance that cannot be spent. Exposing the
        minimum lets the dashboard say so without revealing organization
        totals to project members.
        """
        customer_credit = CustomerCredit.objects.filter(
            customer=self.project.customer
        ).first()
        if not customer_credit:
            return decimal.Decimal("0")
        return min(self.value, customer_credit.value)

    @property
    def is_limited_by_organization_credit(self) -> bool:
        """True when the organization balance, not this allocation, is binding."""
        return self.spendable_value < self.value

    # Also read by the ledger post_save handler, which needs the previous value
    # to compute the delta it records.
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "project__customer"
        # Read access for project roles too. The project dashboard renders
        # credit-adjusted costs and policy saturation to every project member,
        # but the credit object itself was customer-scoped, so members saw an
        # empty list — indistinguishable from "this project has no credit" —
        # and the credit panel silently disappeared. Mirrors ProjectPolicy,
        # which is customer- and project-visible for the same reason. Writes
        # stay owner/staff-only: they are guarded by the ViewSet's
        # update/partial_update/destroy permissions, not by this filter.
        project_path = "project"

    def __str__(self):
        return f"Project credit for {self.project.name}, value {self.value}."

    def save(self, *args, **kwargs):
        customer_credit = CustomerCredit.objects.filter(
            customer=self.project.customer
        ).first()

        if not customer_credit:
            raise rf_exceptions.ValidationError(_("Customer credit does not exist."))

        total_value = (
            ProjectCredit.objects.filter(project__customer=self.project.customer)
            .exclude(pk=self.pk)
            .aggregate(sum=Sum("value"))["sum"]
            or 0 + self.value
        )

        if total_value > customer_credit.value:
            raise rf_exceptions.ValidationError(
                _(
                    "The sum of project credits cannot exceed the credit for organization."
                )
            )

        return super().save(*args, **kwargs)


class CreditTransaction(core_models.UuidMixin, models.Model):
    """Append-only ledger of credit value changes, organization and project.

    Rows are written by the ``record_credit_transaction`` post_save handler
    for every value mutation; the semantic type comes from the innermost
    ``ledger.credit_transaction_type`` block (staff grant when untyped).
    The ledger is the source of truth for the withdrawable balance, so it
    must never be edited or deleted — corrections are new rows.
    """

    class Types:
        STAFF_GRANT = "staff_grant"
        COMPENSATION = "compensation"
        MINIMAL_DRAW = "minimal_draw"
        AFFILIATE_FEE = "affiliate_fee"
        TRANSFER_IN = "transfer_in"
        TRANSFER_OUT = "transfer_out"
        PAYOUT = "payout"
        EXPIRY = "expiry"
        ROLLBACK = "rollback"
        ADJUSTMENT = "adjustment"
        WITHDRAWABLE_ADJUSTMENT = "withdrawable_adjustment"

        CHOICES = (
            (STAFF_GRANT, "Staff grant"),
            (COMPENSATION, "Compensation"),
            (MINIMAL_DRAW, "Minimal consumption draw"),
            (AFFILIATE_FEE, "Affiliate fee"),
            (TRANSFER_IN, "Transfer in"),
            (TRANSFER_OUT, "Transfer out"),
            (PAYOUT, "Payout"),
            (EXPIRY, "Expiry"),
            (ROLLBACK, "Rollback"),
            (ADJUSTMENT, "Adjustment"),
            (WITHDRAWABLE_ADJUSTMENT, "Withdrawable adjustment"),
        )

        # Inflows that an organization earned (as opposed to was granted)
        # and outflows drawing on them, plus manual staff adjustments of the
        # withdrawable part; their ledger sum, capped by the current credit
        # value, is the withdrawable balance.
        WITHDRAWABLE_TYPES = (
            AFFILIATE_FEE,
            TRANSFER_IN,
            TRANSFER_OUT,
            PAYOUT,
            WITHDRAWABLE_ADJUSTMENT,
        )

    credit = models.ForeignKey(
        CustomerCredit,
        on_delete=models.CASCADE,
        related_name="transactions",
        null=True,
        blank=True,
    )
    # Project allocations are drawn on their own — for usage and, separately, to
    # reach the minimal-consumption floor — and neither movement touches the
    # organization balance, so they are ledgered in their own rows.
    project_credit = models.ForeignKey(
        "ProjectCredit",
        on_delete=models.SET_NULL,
        related_name="transactions",
        null=True,
        blank=True,
    )
    # Denormalised so the trace survives its project: ProjectCredit is deleted
    # with the project, and a ledger that loses its attribution on a delete is
    # not a ledger.
    project_uuid = models.CharField(max_length=32, blank=True, db_index=True)
    project_name = models.CharField(
        max_length=structure_models.PROJECT_NAME_LENGTH, blank=True
    )
    # First day of the month the movement belongs to. A real column because the
    # dashboards group by month, and `reference` is a GenericForeignKey that SQL
    # cannot group on.
    billing_period = models.DateField(null=True, blank=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    # Signed delta applied to CustomerCredit.value.
    amount = models.DecimalField(max_digits=16, decimal_places=5)
    transaction_type = models.CharField(max_length=30, choices=Types.CHOICES)
    # Free-text note; required for manual staff adjustments.
    comment = models.TextField(blank=True, default="")
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.SET_NULL, null=True, blank=True
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    reference = GenericForeignKey("content_type", "object_id")

    @property
    def customer(self):
        """The organization the movement belongs to, whichever balance moved.

        None once a project allocation has been deleted: the row keeps the
        project it names, but the path back to the organization went with the
        allocation. Staff still see such rows; nobody else can, which is the
        same answer the permission paths below give.
        """
        if self.credit_id:
            return self.credit.customer
        if self.project_credit_id:
            return self.project_credit.project.customer
        return None

    class Permissions:
        # Both balances, because a row moves exactly one of them. An
        # organization owner reads the whole ledger of their organization;
        # project roles read their own project's drawdown, mirroring
        # ProjectCredit itself, which they can already read so that the project
        # dashboard has something to render.
        customer_path = ("credit__customer", "project_credit__project__customer")
        project_path = "project_credit__project"

    class Meta:
        ordering = ["-created", "id"]

    @classmethod
    def get_url_name(cls):
        return "credit-transaction"

    def __str__(self):
        # Project rows carry no organization credit, and a row outlives the
        # allocation it describes, so the scope is whichever of the three is
        # still there to name.
        if self.credit_id:
            scope = self.credit.customer.name
        elif self.project_name:
            scope = self.project_name
        else:
            scope = "a removed project"
        return f"{self.get_transaction_type_display()} of {self.amount} for {scope}"


class CustomerAffiliate(core_models.UuidMixin, core_models.TimeStampedModel):
    """Staff-configured link granting the affiliate organization a fixed
    percentage fee from every finalized invoice of the linked customer. All
    fields are writable by staff only; the affiliate organization sees its
    own links read-only.
    """

    customer = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.CASCADE,
        related_name="affiliate_links",
    )
    affiliate = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.CASCADE,
        related_name="affiliate_terms",
    )
    # Percentage of the invoice net price (pre-tax, post-compensation), never
    # of the tax-inclusive total.
    fee_percent = models.DecimalField(
        default=0,
        validators=[
            MinValueValidator(decimal.Decimal("0")),
            MaxValueValidator(decimal.Decimal("100")),
        ],
        max_digits=8,
        decimal_places=5,
    )
    is_active = models.BooleanField(default=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "affiliate"

    class Meta:
        unique_together = ("customer", "affiliate")
        ordering = ["created", "id"]

    @classmethod
    def get_url_name(cls):
        return "customer-affiliate"

    def is_active_on(self, date: datetime.date) -> bool:
        if not self.is_active:
            return False
        if self.start_date and date < self.start_date:
            return False
        if self.end_date and date >= self.end_date:
            return False
        return True

    def calculate_fee(self, amount: decimal.Decimal) -> decimal.Decimal:
        """Return the fee earned from an invoice net price."""
        fee = amount * self.fee_percent / 100
        return max(decimal.Decimal("0"), quantize_price(decimal.Decimal(fee)))

    def __str__(self):
        return f"Affiliate link {self.customer.name} -> {self.affiliate.name}"


class AffiliateFeeAccrual(core_models.UuidMixin, core_models.TimeStampedModel):
    """One fee earned by an affiliate from one finalized invoice.

    This is the only object crossing the boundary between the referred
    customer and the affiliate organization: affiliate-facing serializers
    expose the amount and the invoice period but never the invoice itself.
    The unique constraint makes fee accrual idempotent across re-runs of
    invoice finalization.
    """

    affiliate_link = models.ForeignKey(
        CustomerAffiliate, on_delete=models.CASCADE, related_name="accruals"
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name="affiliate_fee_accruals"
    )
    amount = models.DecimalField(max_digits=16, decimal_places=5)

    class Permissions:
        customer_path = "affiliate_link__affiliate"

    class Meta:
        unique_together = ("affiliate_link", "invoice")
        ordering = ["-created", "id"]

    @classmethod
    def get_url_name(cls):
        return "affiliate-fee-accrual"

    def __str__(self):
        return (
            f"Affiliate fee {self.amount} for {self.affiliate_link.affiliate.name} "
            f"from invoice {self.invoice.year}-{self.invoice.month}"
        )


class PeriodMixin(models.Model):
    class Periods:
        TOTAL = 1
        MONTH_1 = 2
        MONTH_3 = 3
        MONTH_12 = 4

        CHOICES = (
            (TOTAL, "Total"),
            (MONTH_1, "1 month"),
            (MONTH_3, "3 month"),
            (MONTH_12, "12 month"),
        )

    period = FSMIntegerField(default=Periods.MONTH_1, choices=Periods.CHOICES)

    def get_start_date(self):
        if self.period in (
            self.Periods.MONTH_1,
            self.Periods.MONTH_3,
            self.Periods.MONTH_12,
        ):
            start = core_utils.month_start(datetime.date.today())

            if self.period == self.Periods.MONTH_3:
                start = core_utils.month_start(
                    datetime.date.today() - relativedelta(months=2)
                )
            elif self.period == self.Periods.MONTH_12:
                start = core_utils.month_start(
                    datetime.date.today() - relativedelta(months=11)
                )

            return start

    class Meta:
        abstract = True


reversion.register(InvoiceItem)
reversion.register(Invoice, follow=("items",))
reversion.register(CustomerCredit)
reversion.register(ProjectCredit)
