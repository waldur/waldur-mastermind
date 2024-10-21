import datetime
import decimal
import logging
from calendar import monthrange

from dateutil.parser import parse as parse_datetime
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.aggregates import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from rest_framework import exceptions as rf_exceptions
from reversion import revisions as reversion

from waldur_core.core import models as core_models
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.marketplace import models as marketplace_models

from . import log, utils

logger = logging.getLogger(__name__)

Units = common_mixins.UnitPriceMixin.Units


def get_created_date():
    now = timezone.now()
    return datetime.date(now.year, now.month, 1)


class Invoice(core_models.UuidMixin, core_models.BackendMixin, models.Model):
    """Invoice describes billing information about purchased resources for customers on a monthly basis"""

    class Permissions:
        customer_path = "customer"

    class Meta:
        unique_together = ("customer", "month", "year")

    class States:
        PENDING = "pending"
        CREATED = "created"
        PAID = "paid"
        CANCELED = "canceled"

        CHOICES = (
            (PENDING, _("Pending")),
            (CREATED, _("Created")),
            (PAID, _("Paid")),
            (CANCELED, _("Canceled")),
        )

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

    tracker = FieldTracker()

    def update_cache(self):
        current_total = self.total

        if self.total_cost != current_total:
            self.total_cost = current_total
            self.save(update_fields=["total_cost"])

        current_price = self.price

        if self.total_price != current_price:
            self.total_price = current_price
            self.save(update_fields=["total_price"])

    @property
    def tax(self):
        return self.price * self.tax_percent / 100

    @property
    def total(self):
        return self.price + self.tax

    @property
    def price(self):
        return quantize_price(
            decimal.Decimal(sum(item.price for item in self.items.all()))
        )

    @property
    def tax_current(self):
        return self.price_current * self.tax_percent / 100

    @property
    def total_current(self):
        return self.price_current + self.tax_current

    @property
    def price_current(self):
        return sum(item.price_current for item in self.items.all())

    @property
    def due_date(self):
        if self.invoice_date:
            return self.invoice_date + datetime.timedelta(
                days=settings.WALDUR_INVOICES["PAYMENT_INTERVAL"]
            )

    @property
    def number(self):
        return 100000 + self.id

    def _process_credits(self):
        with transaction.atomic():
            monthly_compensation = utils.MonthlyCompensation(self.customer)
            monthly_compensation.save()

            if monthly_compensation.tail:
                log.event_logger.credit.info(
                    "Reduction of {customer_name} credit by {consumption} due to minimal consumption of {minimal_consumption}",
                    event_type="reduction_of_credit_due_to_minimal_consumption",
                    event_context={
                        "consumption": monthly_compensation.tail,
                        "minimal_consumption": monthly_compensation.credit.minimal_consumption,
                        "customer": self.customer,
                    },
                )

            for compensation_item in monthly_compensation.compensations:
                log.event_logger.credit.info(
                    "Reduction of {customer_name} credit by {consumption} due to compensation of invoice item {invoice_item}.",
                    event_type="reduction_of_credit",
                    event_context={
                        "consumption": compensation_item.unit_price,
                        "customer": self.customer,
                        "invoice_item": str(compensation_item),
                    },
                )

    def set_created(self):
        """
        Change state from pending to billed
        """
        if self.state != self.States.PENDING:
            raise IncorrectStateException(_("Invoice must be in pending state."))

        self._process_credits()

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


def get_quantity(unit, start, end):
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


class InvoiceItem(
    core_models.UuidMixin, common_mixins.ProductCodeMixin, common_mixins.UnitPriceMixin
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
    name = models.TextField(default="")
    details = models.JSONField(
        default=dict, blank=True, help_text=_("Stores data about scope")
    )

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
    credit = models.ForeignKey(
        "CustomerCredit", on_delete=models.SET_NULL, null=True, editable=False
    )

    tracker = FieldTracker()

    @property
    def tax(self):
        return self.price * self.invoice.tax_percent / 100

    @property
    def tax_current(self):
        return self.price_current * self.invoice.tax_percent / 100

    @property
    def total(self):
        return self.price + self.tax

    def _price(self, current=False):
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

    def get_measured_unit(self):
        if self.measured_unit:
            return self.measured_unit

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

    def get_project_uuid(self):
        if self.project_uuid:
            return self.project_uuid
        return self.project.uuid

    def get_project_name(self):
        if self.project_name:
            return self.project_name
        if self.project:
            return self.project.name
        return "N/A"

    @property
    def price(self):
        return self._price()

    @property
    def price_current(self):
        return self._price(current=True)

    def get_plan_component(self):
        plan_component_id = self.details.get("plan_component_id")
        if not plan_component_id:
            return
        try:
            return marketplace_models.PlanComponent.objects.get(id=plan_component_id)
        except marketplace_models.PlanComponent.DoesNotExist:
            return

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
        if (
            plan_component.component.billing_type
            == marketplace_models.OfferingComponent.BillingTypes.FIXED
            or (
                plan_component.component.billing_type
                == marketplace_models.OfferingComponent.BillingTypes.LIMIT
                and plan_component.component.limit_period
                != marketplace_models.OfferingComponent.LimitPeriods.TOTAL
            )
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

    def __str__(self):
        return self.name or "<InvoiceItem %s>" % self.pk


class PaymentType(models.CharField):
    FIXED_PRICE = "fixed_price"
    MONTHLY_INVOICES = "invoices"
    PAYMENT_GW_MONTHLY = "payment_gw_monthly"

    CHOICES = (
        (FIXED_PRICE, "Fixed-price contract"),
        (MONTHLY_INVOICES, "Monthly invoices"),
        (PAYMENT_GW_MONTHLY, " Payment gateways (monthly)"),
    )

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 30
        kwargs["choices"] = self.CHOICES
        super().__init__(*args, **kwargs)


class PaymentProfile(core_models.UuidMixin, core_models.NameMixin, models.Model):
    organization = models.ForeignKey("structure.Customer", on_delete=models.PROTECT)
    payment_type = PaymentType()
    attributes = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(null=True, default=True)

    tracker = FieldTracker()

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


class CustomerCredit(core_models.UuidMixin, core_models.TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)
    value = models.DecimalField(
        default=0,
        validators=[MinValueValidator(decimal.Decimal("0"))],
        max_digits=11,
        decimal_places=5,
    )
    offerings = models.ManyToManyField(marketplace_models.Offering)
    end_date = models.DateField(null=True)
    minimal_consumption = models.DecimalField(
        default=0,
        validators=[MinValueValidator(decimal.Decimal("0"))],
        max_digits=11,
        decimal_places=5,
    )

    tracker = FieldTracker()

    class Permissions:
        customer_path = "customer"

    @property
    def allocated_to_projects(self):
        return (
            ProjectCredit.objects.filter(project__customer=self.customer).aggregate(
                sum=Sum("value")
            )["sum"]
            or 0
        )

    def __str__(self):
        return f"Customer credit for {self.customer.name}, value {self.value}"


class ProjectCredit(core_models.UuidMixin, core_models.TimeStampedModel):
    project = models.OneToOneField(structure_models.Project, on_delete=models.CASCADE)
    value = models.DecimalField(
        default=0,
        validators=[MinValueValidator(decimal.Decimal("0"))],
        max_digits=11,
        decimal_places=5,
    )
    use_organisation_credit = models.BooleanField(default=True)

    class Permissions:
        customer_path = "project__customer"

    def __str__(self):
        return f"Project credit for {self.project.name}, value {self.value}, organisation credit usage: {self.use_organisation_credit}"

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


reversion.register(InvoiceItem)
reversion.register(Invoice, follow=("items",))
