from __future__ import unicode_literals

from decimal import Decimal

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible
from jsonfield import JSONField

from nodeconductor.core import models as core_models
from nodeconductor.core.exceptions import IncorrectStateException
from nodeconductor.structure import models as structure_models

from nodeconductor_assembly_waldur.packages import models as package_models
from . import utils, managers


@python_2_unicode_compatible
class Invoice(core_models.UuidMixin, models.Model):
    """ Invoice describes billing information about purchased packages for customers on a monthly basis """

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        unique_together = ('customer', 'month', 'year')

    class States(object):
        BILLED = 'billed'
        PAID = 'paid'
        PENDING = 'pending'

        CHOICES = ((BILLED, 'Billed'), (PAID, 'Paid'), (PENDING, 'Pending'))

    month = models.PositiveSmallIntegerField(default=utils.get_current_month,
                                             validators=[MinValueValidator(1), MaxValueValidator(12)])
    year = models.PositiveSmallIntegerField(default=utils.get_current_year)
    state = models.CharField(max_length=7, choices=States.CHOICES, default=States.PENDING)
    customer = models.ForeignKey(structure_models.Customer, related_name='+')

    objects = managers.InvoiceManager()

    @property
    def total(self):
        return self.openstack_items.aggregate(total=models.Sum('price'))['total']

    def set_billed(self):
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

        self.state = self.States.BILLED
        self.save(update_fields=['state'])

    def propagate(self, month, year):
        self.set_billed()
        Invoice.objects.get_or_create_with_items(customer=self.customer, month=month, year=year)

    def __str__(self):
        return '%s | %s-%s' % (self.customer, self.year, self.month)


@python_2_unicode_compatible
class OpenStackItem(models.Model):
    """ OpenStackItem stores details for invoices about purchased OpenStack packages """

    invoice = models.ForeignKey(Invoice, related_name='openstack_items')

    package = models.ForeignKey(package_models.OpenStackPackage, on_delete=models.SET_NULL, null=True, related_name='+')
    package_details = JSONField(default={}, blank=True, help_text='Stores data about package')
    price = models.DecimalField(max_digits=13, decimal_places=7, validators=[MinValueValidator(Decimal('0'))],
                                help_text='Price is calculated on a monthly basis.')
    start = models.DateTimeField(default=utils.get_current_month_start,
                                 help_text='Date and time when package usage has started.')
    end = models.DateTimeField(default=utils.get_current_month_end,
                               help_text='Date and time when package usage has ended.')

    objects = managers.OpenStackItemManager()

    @property
    def name(self):
        if self.package:
            return '%s (%s)' % (self.package.tenant.name, self.package.template.name)

        return '%s (%s)' % (self.package_details.get('tenant_name'), self.package_details.get('template_name'))

    @staticmethod
    def calculate_price_for_period(price, start, end):
        """ Calculates price from "start" till "end" """
        seconds_in_day = 24 * 60 * 60
        full_days, extra_seconds = divmod((end - start).total_seconds(), seconds_in_day)
        if extra_seconds > 0:
            full_days += 1
        return price * 24 * int(full_days)

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
            self.price = self.calculate_price_for_period(self.package.template.price, self.start, self.end)
            update_fields.extend(['end', 'price'])

        self.save(update_fields=update_fields)

    def recalculate_price(self, start):
        """
        Updates price according to the new "start"
        """
        self.start = start
        self.price = self.calculate_price_for_period(self.package.template.price, self.start, self.end)
        self.save(update_fields=['start', 'price'])

    def __str__(self):
        return self.name
