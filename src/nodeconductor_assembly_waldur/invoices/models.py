from __future__ import unicode_literals

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible
from jsonfield import JSONField

from nodeconductor.core import models as core_models
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
            - freezes all invoice items
            - changes state from pending to billed
        """
        if self.state != self.States.PENDING:
            raise ValidationError('Invoice must be in pending state.')

        # XXX: Consider refactoring when different types of packages will be exposed.
        items = self.openstack_items.select_related('package').all()
        for item in items:
            if item.package:
                item.freeze()

        self.state = self.States.BILLED
        self.save(update_fields=['state'])

    def propagate(self):
        self.set_billed()
        Invoice.objects.create(self.customer)

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
    start = models.DateTimeField(default=utils.get_current_month_start_datetime,
                                 help_text='Date and time when package usage has started.')
    end = models.DateTimeField(default=utils.get_current_month_end_datetime,
                               help_text='Date and time when package usage has ended.')

    objects = managers.OpenStackItemManager()

    @property
    def name(self):
        name = self.package_details.get('name')
        if name:
            return name
        elif self.package:
            return '%s (%s)' % (self.package.tenant.name, self.package.template.name)

    def freeze(self, package_deletion=False):
        """
        Performs following actions:
            - saves name in package_details in format "<package tenant name> (<package template name>)"
            - if package_deletion is set to True, then sets end field as current timestamp and
              recalculates price based on the new end field.
        """
        self.package_details['name'] = '%s (%s)' % (self.package.tenant.name, self.package.template.name)
        update_fields = ['package_details']

        if package_deletion:
            self.end = timezone.now()
            self.price = self.package.template.price * 24 * (self.end - self.start).days
            update_fields.extend(['end', 'price'])

        self.save(update_fields=update_fields)

    def __str__(self):
        return self.name
