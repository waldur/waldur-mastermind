import base64
from datetime import timedelta
import decimal
from io import BytesIO
import logging
from calendar import monthrange

import datetime
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices.utils import get_price_per_day
from waldur_mastermind.packages import models as package_models

from . import managers, utils

logger = logging.getLogger(__name__)

Units = common_mixins.UnitPriceMixin.Units


class Invoice(core_models.UuidMixin, models.Model):
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

        CHOICES = ((PENDING, _('Pending')), (CREATED, _('Created')), (PAID, _('Paid')), (CANCELED, _('Canceled')))

    month = models.PositiveSmallIntegerField(default=utils.get_current_month,
                                             validators=[MinValueValidator(1), MaxValueValidator(12)])
    year = models.PositiveSmallIntegerField(default=utils.get_current_year)
    state = models.CharField(max_length=30, choices=States.CHOICES, default=States.PENDING)
    customer = models.ForeignKey(structure_models.Customer,
                                 verbose_name=_('organization'),
                                 related_name='+',
                                 on_delete=models.CASCADE)
    current_cost = models.DecimalField(default=0, max_digits=10, decimal_places=2,
                                       help_text=_('Cached value for current cost.'),
                                       editable=False)
    tax_percent = models.DecimalField(default=0, max_digits=4, decimal_places=2,
                                      validators=[MinValueValidator(0), MaxValueValidator(100)])
    invoice_date = models.DateField(null=True, blank=True,
                                    help_text=_('Date then invoice moved from state pending to created.'))
    _file = models.TextField(blank=True, editable=False)

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
        return sum((item.price for item in self.items))

    @property
    def tax_current(self):
        return self.price_current * self.tax_percent / 100

    @property
    def total_current(self):
        return self.price_current + self.tax_current

    @property
    def price_current(self):
        return sum((item.price_current for item in self.items))

    @property
    def items(self):
        return self.generic_items.all()

    @property
    def due_date(self):
        if self.invoice_date:
            return self.invoice_date + datetime.timedelta(days=settings.WALDUR_INVOICES['PAYMENT_INTERVAL'])

    @property
    def number(self):
        return 100000 + self.id

    def set_created(self):
        """
        Change state from pending to billed
        """
        if self.state != self.States.PENDING:
            raise IncorrectStateException(_('Invoice must be in pending state.'))

        self.state = self.States.CREATED
        self.invoice_date = timezone.now().date()
        self.save(update_fields=['state', 'invoice_date'])

    @property
    def file(self):
        if not self._file:
            return

        content = base64.b64decode(self._file)
        return BytesIO(content)

    @file.setter
    def file(self, value):
        self._file = value

    def has_file(self):
        return bool(self._file)

    def get_filename(self):
        return 'invoice_{}.pdf'.format(self.uuid)

    def __str__(self):
        return '%s | %s-%s' % (self.customer, self.year, self.month)


class InvoiceItem(common_mixins.ProductCodeMixin, common_mixins.UnitPriceMixin):
    """
    It is expected that get_scope_type method is defined as class method in scope class
    as it is used in generic invoice item serializer.
    """
    invoice = models.ForeignKey(on_delete=models.CASCADE, to=Invoice, related_name='generic_items')
    content_type = models.ForeignKey(on_delete=models.CASCADE, to=ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    quantity = models.PositiveIntegerField(default=0)

    scope = GenericForeignKey('content_type', 'object_id')
    name = models.TextField(default='')
    details = JSONField(default=dict, blank=True, help_text=_('Stores data about scope'))

    start = models.DateTimeField(default=utils.get_current_month_start,
                                 help_text=_('Date and time when item usage has started.'))
    end = models.DateTimeField(default=utils.get_current_month_end,
                               help_text=_('Date and time when item usage has ended.'))

    # Project name and UUID should be stored separately because project is not available after removal
    project = models.ForeignKey(structure_models.Project, on_delete=models.SET_NULL, null=True)
    project_name = models.CharField(max_length=150, blank=True)
    project_uuid = models.CharField(max_length=32, blank=True)

    objects = managers.InvoiceItemManager()
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
        return self.unit_price * decimal.Decimal(self.get_factor(current))

    def get_factor(self, current=False):
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
            if (self.start.day == 1 and self.end.day == 15) or (self.start.day == 16 and self.end.day == month_days):
                return 1
            elif (self.start.day == 1 and self.end.day == month_days):
                return 2
            elif (self.start.day == 1 and self.end.day > 15):
                return quantize_price(1 + (self.end.day - 15) / decimal.Decimal(month_days / 2))
            elif (self.start.day < 16 and self.end.day == month_days):
                return quantize_price(1 + (16 - self.start.day) / decimal.Decimal(month_days / 2))
            else:
                return quantize_price((self.end.day - self.start.day + 1) / decimal.Decimal(month_days / 2.0))
        # By default PER_MONTH
        else:
            if self.start.day == 1 and self.end.day == month_days:
                return 1

            use_days = (self.end - self.start).days + 1
            return quantize_price(decimal.Decimal(use_days) / month_days)

    @property
    def price(self):
        return self._price()

    @property
    def price_current(self):
        return self._price(current=True)

    @property
    def usage_days(self):
        """
        Returns the number of days package was used from the time
        it was purchased or from the start of current month
        """
        full_days = utils.get_full_days(self.start, self.end)
        return full_days

    def terminate(self, end=None):
        self.end = end or timezone.now()
        self.save(update_fields=['end'])

    def __str__(self):
        return self.name or '<InvoiceItem %s>' % self.pk

    def create_compensation(self, name, **kwargs):
        FIELDS = (
            'invoice',
            'project',
            'project_name',
            'project_uuid',
            'product_code',
            'article_code',
            'unit',
            'unit_price',
            'start',
            'end',
        )

        params = {field: getattr(self, field) for field in FIELDS}
        params.update(kwargs)
        if params['unit_price'] > 0:
            params['unit_price'] *= -1
        params['details'] = {
            'name': _('Compensation for downtime. Resource name: %s') % name
        }

        return InvoiceItem.objects.create(**params)


def get_default_downtime_start():
    return timezone.now() - settings.WALDUR_INVOICES['DOWNTIME_DURATION_MINIMAL']


class ServiceDowntime(models.Model):
    """
    Currently this model is restricted to OpenStack package only.
    It is expected that implementation would be generalized to support other resources as well.
    """
    start = models.DateTimeField(
        default=get_default_downtime_start,
        help_text=_('Date and time when downtime has started.')
    )
    end = models.DateTimeField(
        default=timezone.now,
        help_text=_('Date and time when downtime has ended.')
    )
    package = models.ForeignKey(on_delete=models.CASCADE, to=package_models.OpenStackPackage)

    def clean(self):
        self._validate_duration()
        self._validate_offset()
        self._validate_intersection()

    def _validate_duration(self):
        duration = self.end - self.start

        duration_min = settings.WALDUR_INVOICES['DOWNTIME_DURATION_MINIMAL']
        if duration_min is not None and duration < duration_min:
            raise ValidationError(
                _('Downtime duration is too small. Minimal duration is %s') % duration_min
            )

        duration_max = settings.WALDUR_INVOICES['DOWNTIME_DURATION_MAXIMAL']
        if duration_max is not None and duration > duration_max:
            raise ValidationError(
                _('Downtime duration is too big. Maximal duration is %s') % duration_max
            )

    def _validate_offset(self):
        if self.start > timezone.now() or self.end > timezone.now():
            raise ValidationError(
                _('Future downtime is not supported yet. '
                  'Please select date in the past instead.')
            )

    def get_intersection_subquery(self):
        left = Q(start__gte=self.start, start__lte=self.end)
        right = Q(end__gte=self.start, end__lte=self.end)
        inside = Q(start__gte=self.start, end__lte=self.end)
        outside = Q(start__lte=self.start, end__gte=self.end)
        return Q(left | right | inside | outside)

    def _validate_intersection(self):
        qs = ServiceDowntime.objects.filter(self.get_intersection_subquery(), package=self.package)
        if qs.exists():
            ids = ', '.join(str(item.id) for item in qs)
            raise ValidationError(
                _('Downtime period intersects with another period with ID: %s.') % ids
            )


class InvoiceItemAdjuster:
    def __init__(self, invoice, source, start, unit_price, unit):
        self.invoice = invoice
        self.source = source
        self.start = start
        self.unit_price = unit_price
        self.unit = unit

    @cached_property
    def content_type(self):
        return ContentType.objects.get_for_model(self.source)

    @property
    def invoice_items(self):
        # TODO: Remove temporary workaround for OpenStack package
        if isinstance(self.source, package_models.OpenStackPackage):
            return InvoiceItem.objects.filter(
                invoice=self.invoice,
                content_type=self.content_type,
                details__tenant_name=self.source.tenant.name,
            )
        return InvoiceItem.objects.filter(
            invoice=self.invoice,
            content_type=self.content_type,
            object_id=self.source.pk,
        )

    @cached_property
    def old_item(self):
        qs = self.invoice_items
        if self.unit == Units.PER_DAY:
            qs = qs.filter(end__day=self.start.day)
        elif self.unit == Units.PER_HOUR:
            qs = qs.filter(end__day=self.start.day, end__hour=self.start.hour)
        elif self.unit == Units.PER_MONTH:
            qs = qs.filter(end__month=self.start.month)
        elif self.unit == Units.PER_HALF_MONTH:
            if self.start.day <= 15:
                qs = qs.filter(end__day__lte=15)
            else:
                qs = qs.filter(end__day__gt=15)
        else:
            qs = qs.none()
        return qs.order_by('-unit_price').first()

    @property
    def old_price(self):
        return get_price_per_day(self.old_item.unit_price, self.old_item.unit)

    @property
    def new_price(self):
        return get_price_per_day(self.unit_price, self.unit)

    def shift_forward(self):
        """
        Adjust old invoice item end field to the end of current unit.
        Adjust new invoice item start field to the start of next unit.
        """
        end = self.old_item.end

        if self.old_item.unit != self.unit and self.unit == Units.PER_MONTH:
            end = core_utils.month_end(end)
        elif self.old_item.unit != self.unit and self.unit == Units.PER_HALF_MONTH:
            if end.day > 15:
                end = core_utils.month_end(end)
            else:
                end = end.replace(day=15)
        elif self.unit == Units.PER_HOUR:
            end = end.replace(minute=59, second=59)
        else:
            end = end.replace(hour=23, minute=59, second=59)

        start = end + timedelta(seconds=1)
        return start, end

    def shift_backward(self):
        """
        Adjust old invoice item end field to the end of previous unit
        Adjust new invoice item field to the start of current unit.
        """
        end = self.old_item.end

        if self.old_item.unit != self.unit and self.unit == Units.PER_MONTH:
            start = core_utils.month_start(end)
        elif self.old_item.unit != self.unit and self.unit == Units.PER_HALF_MONTH:
            if end.day < 15:
                start = core_utils.month_start(end)
            else:
                start = end.replace(day=15)
        elif self.unit == Units.PER_HOUR:
            start = end.replace(minute=0, second=0)
        else:
            start = end.replace(hour=0, minute=0, second=0)

        end = start - timedelta(seconds=1)
        return start, end

    def remove_new_items(self, start):
        """
        Cleanup planned invoice items when new item is created.
        """
        qs = self.invoice_items
        if self.unit == Units.PER_DAY:
            qs = qs.filter(start__day=start.day)
        elif self.unit == Units.PER_HOUR:
            qs = qs.filter(start__day=start.day, start__hour=start.hour)
        else:
            qs = qs.none()

        qs.delete()

    def adjust(self):
        start = self.start

        if self.old_item and self.old_item.price > 0:
            if self.old_price >= self.new_price:
                start, end = self.shift_forward()
            else:
                start, end = self.shift_backward()

            self.old_item.end = end
            self.old_item.save(update_fields=['end'])

        self.remove_new_items(start)

        return start


def adjust_invoice_items(invoice, source, start, unit_price, unit):
    """
    When resource configuration is switched, old invoice item
    is terminated and new invoice item is created.
    In order to avoid double counting we should ensure that
    there're no overlapping invoice items for the same scope.

    By default daily prorate is used even if plan is monthly or half-monthly.
    Two notable exceptions are:
    1) Switching from daily plan to monthly.
    2) Switching between hourly plans.
    """
    return InvoiceItemAdjuster(invoice, source, start, unit_price, unit).adjust()
