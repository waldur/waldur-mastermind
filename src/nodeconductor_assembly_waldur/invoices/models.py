from __future__ import unicode_literals

from decimal import Decimal

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible

from nodeconductor.core import models as core_models
from nodeconductor.core.validators import validate_name
from nodeconductor.structure import models as structure_models

from nodeconductor_assembly_waldur.packages import models as package_models
from . import utils


@python_2_unicode_compatible
class Invoice(core_models.UuidMixin, models.Model):
    """ Invoice describes billing information about purchased packages for customers on a monthly basis """

    class Permissions(object):
        customer_path = 'customer'

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

    @property
    def total(self):
        return self.items.aggregate(total=models.Sum('price'))['total']

    def __str__(self):
        return '%s | %s-%s' % (self.customer, self.year, self.month)


@python_2_unicode_compatible
class InvoiceItem(models.Model):
    """ InvoiceItem stores details for invoices about purchased OpenStack packages """

    invoice = models.ForeignKey(Invoice, related_name='items')

    package = models.ForeignKey(package_models.OpenStackPackage, on_delete=models.SET_NULL, null=True)
    template_name = models.CharField(max_length=150, validators=[validate_name], blank=True,
                                     help_text='Stores name of the package template after package deletion.')
    tenant_name = models.CharField(max_length=150, validators=[validate_name], blank=True,
                                   help_text='Stores name of the tenant after package deletion.')
    price = models.DecimalField(max_digits=13, decimal_places=7, validators=[MinValueValidator(Decimal('0'))],
                                help_text='Price is calculated on a monthly basis.')
    start = models.DateTimeField(default=utils.get_current_month_start_datetime,
                                 help_text='Date and time when package usage has started.')
    end = models.DateTimeField(default=utils.get_current_month_end_datetime,
                               help_text='Date and time when package usage has ended.')

    def __str__(self):
        return self.tenant_name
