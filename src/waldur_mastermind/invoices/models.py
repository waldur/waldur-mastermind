import datetime
import decimal
import logging
from calendar import monthrange

from dateutil.parser import parse as parse_datetime
from django.conf import settings
from django.contrib.postgres.fields import JSONField
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from reversion import revisions as reversion

from waldur_core.core import models as core_models
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.marketplace import models as marketplace_models

from . import utils

logger = logging.getLogger(__name__)

Units = common_mixins.UnitPriceMixin.Units


def get_created_date():
    now = timezone.now()
    return datetime.date(now.year, now.month, 1)


class Invoice(core_models.UuidMixin, core_models.BackendMixin, models.Model):
    """ Invoice describes billing information about purchased resources for customers on a monthly basis """

    class Permissions:
        customer_path = 'customer'

    class Meta:
        unique_together = ('customer', 'month', 'year')

    class States:
        PENDING = 'pending'
        CREATED = 'created'
        PAID = 'paid'
        CANCELED = 'canceled'

        CHOICES = (
            (PENDING, _('Pending')),
            (CREATED, _('Created')),
            (PAID, _('Paid')),
            (CANCELED, _('Canceled')),
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
        verbose_name=_('organization'),
        related_name='+',
        on_delete=models.CASCADE,
    )
    current_cost = models.DecimalField(
        default=0,
        max_digits=10,
        decimal_places=2,
        help_text=_('Cached value for current cost.'),
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
        help_text=_('Date then invoice moved from state pending to created.'),
    )

    tracker = FieldTracker()

    def update_current_cost(self):
        total_current = self.total_current

        if self.current_cost != total_current:
            self.current_cost = total_current
            self.save(update_fields=['current_cost'])

    @property
    def tax(self):
        return self.price * self.tax_percent / 100

    @property
    def total(self):
        return self.price + self.tax

    @property
    def price(self):
        return quantize_price(
            decimal.Decimal(sum((item.price for item in self.items.all())))
        )

    @property
    def tax_current(self):
        return self.price_current * self.tax_percent / 100

    @property
    def total_current(self):
        return self.price_current + self.tax_current

    @property
    def price_current(self):
        return sum((item.price_current for item in self.items.all()))

    @property
    def due_date(self):
        if self.invoice_date:
            return self.invoice_date + datetime.timedelta(
                days=settings.WALDUR_INVOICES['PAYMENT_INTERVAL']
            )

    @property
    def number(self):
        return 100000 + self.id

    def set_created(self):
        """
        Change state from pending to billed
        """
        if self.state != self.States.PENDING:
            raise IncorrectStateException(_('Invoice must be in pending state.'))

        if self.customer.paymentprofile_set.filter(
            is_active=True, payment_type=PaymentType.FIXED_PRICE
        ).count():
            self.state = self.States.PAID
        else:
            self.state = self.States.CREATED

        self.invoice_date = timezone.now().date()
        self.save(update_fields=['state', 'invoice_date'])

    def get_filename(self):
        return 'invoice_{}.pdf'.format(self.uuid)

    def __str__(self):
        return '%s | %s-%s' % (self.customer, self.year, self.month)


class InvoiceItem(
    core_models.UuidMixin, common_mixins.ProductCodeMixin, common_mixins.UnitPriceMixin
):
    """
    It is expected that get_scope_type method is defined as class method in scope class
    as it is used in generic invoice item serializer.
    """

    invoice = models.ForeignKey(
        on_delete=models.CASCADE, to=Invoice, related_name='items'
    )
    quantity = models.PositiveIntegerField(default=0)
    measured_unit = models.CharField(
        max_length=30, help_text=_('Unit of measurement, for example, GB.'), blank=True
    )
    resource = models.ForeignKey(
        on_delete=models.PROTECT,
        to=marketplace_models.Resource,
        related_name='invoice_items',
        null=True,
    )
    name = models.TextField(default='')
    details = JSONField(
        default=dict, blank=True, help_text=_('Stores data about scope')
    )

    start = models.DateTimeField(
        default=utils.get_current_month_start,
        help_text=_('Date and time when item usage has started.'),
    )
    end = models.DateTimeField(
        default=utils.get_current_month_end,
        help_text=_('Date and time when item usage has ended.'),
    )

    # Project name and UUID should be stored separately because project is not available after removal
    project = models.ForeignKey(
        structure_models.Project, on_delete=models.SET_NULL, null=True
    )
    project_name = models.CharField(max_length=150, blank=True)
    project_uuid = models.CharField(max_length=32, blank=True)

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
        return quantize_price(
            self.unit_price * decimal.Decimal(self.get_factor(current))
        )

    def get_factor(self, current=False):
        if self.quantity:
            return self.quantity
        month_days = monthrange(self.start.year, self.start.month)[1]

        if self.unit == self.Units.QUANTITY:
            return self.quantity
        elif self.unit == self.Units.PER_HOUR:
            if current:
                return utils.get_full_hours(self.start, min(self.end, timezone.now()))
            else:
                return utils.get_full_hours(self.start, self.end)
        elif self.unit == self.Units.PER_DAY:
            if current:
                return utils.get_full_days(self.start, min(self.end, timezone.now()))
            else:
                return self.usage_days
        elif self.unit == self.Units.PER_HALF_MONTH:
            if (self.start.day == 1 and self.end.day == 15) or (
                self.start.day == 16 and self.end.day == month_days
            ):
                return 1
            elif self.start.day == 1 and self.end.day == month_days:
                return 2
            elif self.start.day == 1 and self.end.day > 15:
                return quantize_price(
                    1 + (self.end.day - 15) / decimal.Decimal(month_days / 2)
                )
            elif self.start.day < 16 and self.end.day == month_days:
                return quantize_price(
                    1 + (16 - self.start.day) / decimal.Decimal(month_days / 2)
                )
            else:
                return quantize_price(
                    (self.end.day - self.start.day + 1)
                    / decimal.Decimal(month_days / 2.0)
                )
        # By default PER_MONTH
        else:
            if self.start.day == 1 and self.end.day == month_days:
                return 1

            use_days = (self.end - self.start).days + 1
            return quantize_price(decimal.Decimal(use_days) / month_days)

    def get_measured_unit(self):
        if self.measured_unit:
            return self.measured_unit

        plural = self.get_factor() > 1

        if self.unit == self.Units.QUANTITY:
            if not self.resource or not self.resource.scope:
                return ''

            if getattr(self.resource.scope, 'content_type', None):
                meta = self.resource.scope.content_type.model_class()._meta
            else:
                meta = self.resource.scope._meta
            return (
                str(meta.verbose_name_plural).lower()
                if plural
                else str(meta.verbose_name).lower()
            )
        elif self.unit == self.Units.PER_HOUR:
            return _('hours') if plural else _('hour')
        elif self.unit == self.Units.PER_DAY:
            return _('days') if plural else _('day')
        elif self.unit == self.Units.PER_HALF_MONTH:
            return _('percents from half a month')
        else:
            return _('percents from a month')

    def get_project_uuid(self):
        if self.project_uuid:
            return self.project_uuid
        try:
            return structure_models.Project.all_objects.get(id=self.project_id).uuid
        except ObjectDoesNotExist:
            return

    def get_project_name(self):
        if self.project_name:
            return self.project_name
        try:
            return structure_models.Project.all_objects.get(id=self.project_id).name
        except ObjectDoesNotExist:
            return

    @property
    def price(self):
        return self._price()

    @property
    def price_current(self):
        return self._price(current=True)

    @property
    def usage_days(self):
        """
        Returns the number of days resource was used from the time
        it was purchased or from the start of current month
        """
        full_days = utils.get_full_days(self.start, self.end)
        return full_days

    def terminate(self, end=None):
        self.end = end or timezone.now()
        self.save(update_fields=['end'])

        resource_limit_periods = self.details.get('resource_limit_periods')
        if resource_limit_periods:
            last_period = resource_limit_periods[-1]
            last_period['end'] = self.end.isoformat()
            last_period['billing_periods'] = utils.get_full_days(
                parse_datetime(last_period['start']), self.end
            )
            self.save(update_fields=['details'])

    def __str__(self):
        return self.name or '<InvoiceItem %s>' % self.pk


class PaymentType(models.CharField):
    FIXED_PRICE = 'fixed_price'
    MONTHLY_INVOICES = 'invoices'
    PAYMENT_GW_MONTHLY = 'payment_gw_monthly'

    CHOICES = (
        (FIXED_PRICE, 'Fixed-price contract'),
        (MONTHLY_INVOICES, 'Monthly invoices'),
        (PAYMENT_GW_MONTHLY, ' Payment gateways (monthly)'),
    )

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 30
        kwargs['choices'] = self.CHOICES
        super(PaymentType, self).__init__(*args, **kwargs)


class PaymentProfile(core_models.UuidMixin, core_models.NameMixin, models.Model):
    organization = models.ForeignKey('structure.Customer', on_delete=models.PROTECT)
    payment_type = PaymentType()
    attributes = JSONField(default=dict, blank=True)
    is_active = models.NullBooleanField(default=True)

    tracker = FieldTracker()

    def __str__(self):
        return '%s (%s)' % (self.organization.name, self.payment_type)

    class Permissions:
        customer_path = 'organization'

    @classmethod
    def get_url_name(cls):
        return 'payment-profile'

    def save(self, *args, **kwargs):
        if self.is_active is False:
            self.is_active = None

        if not self.tracker.previous(self.is_active) and self.is_active:
            self.__class__.objects.filter(organization=self.organization).exclude(
                pk=self.pk
            ).update(is_active=None)

        return super(PaymentProfile, self).save(*args, **kwargs)

    class Meta:
        unique_together = ('organization', 'is_active')


class Payment(core_models.UuidMixin, core_models.TimeStampedModel):
    profile = models.ForeignKey(
        PaymentProfile, on_delete=models.PROTECT, null=False, blank=False
    )
    sum = models.DecimalField(
        default=0, max_digits=10, decimal_places=2, null=False, blank=False
    )
    date_of_payment = models.DateField(null=False, blank=False,)
    proof = models.FileField(upload_to='proof_of_payment', null=True, blank=True)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Permissions:
        customer_path = 'profile__organization'

    @classmethod
    def get_url_name(cls):
        return 'payment'


reversion.register(InvoiceItem)
reversion.register(Invoice, follow=('items',))
