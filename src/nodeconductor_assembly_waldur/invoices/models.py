from __future__ import unicode_literals

import datetime
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from jsonfield import JSONField
from model_utils import FieldTracker

from nodeconductor.core import models as core_models, utils as core_utils
from nodeconductor.core.exceptions import IncorrectStateException
from nodeconductor.structure import models as structure_models

from nodeconductor_assembly_waldur.packages import models as package_models
from . import utils


@python_2_unicode_compatible
class Invoice(core_models.UuidMixin, models.Model):
    """ Invoice describes billing information about purchased packages for customers on a monthly basis """

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        unique_together = ('customer', 'month', 'year')

    class States(object):
        PENDING = 'pending'
        CREATED = 'created'
        PAID = 'paid'
        CANCELED = 'canceled'

        CHOICES = ((PENDING, 'Pending'), (CREATED, 'Created'), (PAID, 'Paid'), (CANCELED, 'Canceled'))

    month = models.PositiveSmallIntegerField(default=utils.get_current_month,
                                             validators=[MinValueValidator(1), MaxValueValidator(12)])
    year = models.PositiveSmallIntegerField(default=utils.get_current_year)
    state = models.CharField(max_length=30, choices=States.CHOICES, default=States.PENDING)
    customer = models.ForeignKey(structure_models.Customer, related_name='+')
    tax_percent = models.DecimalField(default=0, max_digits=4, decimal_places=2,
                                      validators=[MinValueValidator(0), MaxValueValidator(100)])
    invoice_date = models.DateField(null=True, blank=True,
                                    help_text='Date then invoice moved from state pending to created.')

    tracker = FieldTracker()

    @property
    def tax(self):
        return self.price * self.tax_percent / 100

    @property
    def total(self):
        return self.price + self.tax

    @property
    def price(self):
        return sum((item.price for item in self.openstack_items.iterator()))

    @property
    def due_date(self):
        if self.invoice_date:
            return self.invoice_date + datetime.timedelta(days=settings.INVOICES['PAYMENT_INTERVAL'])

    @property
    def number(self):
        return 100000 + self.id

    def set_created(self):
        """
        Performs following actions:
            - Freeze all invoice items
            - Change state from pending to billed
        """
        if self.state != self.States.PENDING:
            raise IncorrectStateException('Invoice must be in pending state.')

        # XXX: Consider refactoring when different types of packages will be exposed.
        items = self.openstack_items.select_related('package').all()
        for item in items:
            if item.package:
                item.freeze()

        self.state = self.States.CREATED
        self.invoice_date = timezone.now().date()
        self.save(update_fields=['state', 'invoice_date'])

    def register_package(self, package, start=None):
        if start is None:
            start = timezone.now()

        end = core_utils.month_end(start)
        overlapping_item = OpenStackItem.objects.filter(
            invoice=self,
            end__day=start.day,
            package_details__contains=package.tenant.name,
        ).order_by('-daily_price').first()

        daily_price = package.template.price
        if overlapping_item:
            """
            If there is an item that overlaps with current one as shown below:
            |--01.03.2017-|-********-|-***?---|
                                     |----?**-|-01.06.2017-|-******-|
            we have to make next steps:
            1) If item is more expensive -> use it for price calculation
                and register new package starting from next day [-01.06.2017-]
            |--01.03.2017-|-********-|-*****-|
                                     |-------|-01.06.2017-|-******-|

            2) If item is more expensive and it is the end of the month
            repeat step 1 but do not register new package. It will be registered from new month.
            3) If item is cheaper do exactly the opposite and shift its end date to yesterday,
            so new package will be registered today
            |--01.03.2017-|-********-|-------|
                                     |-*****-|-01.06.2017-|-******-|
            """
            if overlapping_item.daily_price > daily_price:
                if overlapping_item.end.day == utils.get_current_month_end().day:
                    overlapping_item.extend_to_the_end_of_the_day()
                    return

                start = start + timezone.timedelta(days=1)
            else:
                overlapping_item.shift_backward()

        OpenStackItem.objects.create(
            package=package,
            daily_price=daily_price,
            invoice=self,
            start=start,
            end=end)

    def __str__(self):
        return '%s | %s-%s' % (self.customer, self.year, self.month)


@python_2_unicode_compatible
class OpenStackItem(models.Model):
    """ OpenStackItem stores details for invoices about purchased OpenStack packages """

    invoice = models.ForeignKey(Invoice, related_name='openstack_items')

    package = models.ForeignKey(package_models.OpenStackPackage, on_delete=models.SET_NULL, null=True, related_name='+')
    package_details = JSONField(default={}, blank=True, help_text='Stores data about package')
    daily_price = models.DecimalField(max_digits=22, decimal_places=7,
                                      validators=[MinValueValidator(Decimal('0'))],
                                      default=0,
                                      help_text='Price per day.')
    start = models.DateTimeField(default=utils.get_current_month_start,
                                 help_text='Date and time when package usage has started.')
    end = models.DateTimeField(default=utils.get_current_month_end,
                               help_text='Date and time when package usage has ended.')

    @property
    def name(self):
        if self.package:
            return '%s (%s)' % (self.package.tenant.name, self.package.template.name)

        return '%s (%s)' % (self.package_details.get('tenant_name'), self.package_details.get('template_name'))

    @property
    def tax(self):
        return self.price * self.invoice.tax_percent / 100

    @property
    def total(self):
        return self.price + self.tax

    @property
    def price(self):
        return self.daily_price * self.usage_days

    @property
    def usage_days(self):
        """
        Returns the number of days package was used from the time
        it was purchased or from the start of current month
        """
        full_days = utils.get_full_days(self.start, self.end)
        return full_days

    def freeze(self, end=None, package_deletion=False):
        """
        Performs following actions:
            - Save tenant and package template names in "package_details"
            - On package deletion set "end" field as "end" and
              recalculate price based on the new "end" field.
        """
        self.package_details['tenant_name'] = self.package.tenant.name
        self.package_details['template_name'] = self.package.template.name
        update_fields = ['package_details']

        if package_deletion:
            self.end = end or timezone.now()
            update_fields.extend(['end'])

        self.save(update_fields=update_fields)

    def shift_backward(self, days=1):
        """
        Shifts end date to N 'days' ago.
        If N is larger than it lasts - zero length will be set.
        :param days: number of days to shift end date
        """
        if (self.end - self.start).days > days:
            end = self.end - timezone.timedelta(days=1)
        else:
            end = self.start

        self.end = end
        self.save()

    def extend_to_the_end_of_the_day(self):
        self.end = self.end.replace(hour=23, minute=59, second=59)
        self.save()

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class PaymentDetails(core_models.UuidMixin, models.Model):
    """ Customer payment details """

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        verbose_name = _('Payment details')
        verbose_name_plural = _('Payment details')

    customer = models.OneToOneField(structure_models.Customer, related_name='payment_details')
    company = models.CharField(blank=True, max_length=150)
    type = models.CharField(blank=True, max_length=150)
    address = models.CharField(blank=True, max_length=300)
    country = models.CharField(blank=True, max_length=50)
    email = models.EmailField(blank=True, max_length=75)
    postal = models.CharField(blank=True, max_length=20)
    phone = models.CharField(blank=True, max_length=20)
    bank = models.CharField(blank=True, max_length=150)
    account = models.CharField(blank=True, max_length=50)
    default_tax_percent = models.DecimalField(default=0, max_digits=4, decimal_places=2,
                                              validators=[MinValueValidator(0), MaxValueValidator(100)])

    @classmethod
    def get_url_name(cls):
        return 'payment-details'

    def __str__(self):
        return 'PaymentDetails for %s' % self.customer
