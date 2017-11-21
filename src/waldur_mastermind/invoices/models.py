from __future__ import unicode_literals

import datetime
import itertools

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible
from django.utils.lru_cache import lru_cache
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from nodeconductor.core.fields import JSONField
from nodeconductor.core import models as core_models, utils as core_utils
from nodeconductor.core.exceptions import IncorrectStateException
from nodeconductor.structure import models as structure_models

from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.support import models as support_models

from . import managers, utils, registrators


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

        CHOICES = ((PENDING, _('Pending')), (CREATED, _('Created')), (PAID, _('Paid')), (CANCELED, _('Canceled')))

    month = models.PositiveSmallIntegerField(default=utils.get_current_month,
                                             validators=[MinValueValidator(1), MaxValueValidator(12)])
    year = models.PositiveSmallIntegerField(default=utils.get_current_year)
    state = models.CharField(max_length=30, choices=States.CHOICES, default=States.PENDING)
    customer = models.ForeignKey(structure_models.Customer, verbose_name=_('organization'), related_name='+')
    tax_percent = models.DecimalField(default=0, max_digits=4, decimal_places=2,
                                      validators=[MinValueValidator(0), MaxValueValidator(100)])
    invoice_date = models.DateField(null=True, blank=True,
                                    help_text=_('Date then invoice moved from state pending to created.'))

    tracker = FieldTracker()

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
    def items(self):
        return itertools.chain(self.openstack_items.all(),
                               self.offering_items.all(),
                               self.generic_items.all())

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
            raise IncorrectStateException(_('Invoice must be in pending state.'))

        self.state = self.States.CREATED
        self.invoice_date = timezone.now().date()
        self.save(update_fields=['state', 'invoice_date'])

    def freeze(self):
        for item in self.items:
            item.freeze()

    def register_offering(self, offering, start=None):
        if start is None:
            start = timezone.now()

        end = core_utils.month_end(start)
        OfferingItem.objects.create(
            offering=offering,
            unit_price=offering.unit_price,
            unit=offering.unit,
            invoice=self,
            start=start,
            end=end,
        )

    def __str__(self):
        return '%s | %s-%s' % (self.customer, self.year, self.month)


@python_2_unicode_compatible
class InvoiceItem(common_mixins.ProductCodeMixin, common_mixins.UnitPriceMixin):
    """
    Mixin which identifies invoice item to be used for price calculation.
    """

    class Meta(object):
        abstract = True

    start = models.DateTimeField(default=utils.get_current_month_start,
                                 help_text=_('Date and time when item usage has started.'))
    end = models.DateTimeField(default=utils.get_current_month_end,
                               help_text=_('Date and time when item usage has ended.'))

    # Project name and UUID should be stored separately because project is not available after removal
    project = models.ForeignKey(structure_models.Project, on_delete=models.SET_NULL, null=True)
    project_name = models.CharField(max_length=150, blank=True)
    project_uuid = models.CharField(max_length=32, blank=True)

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @property
    def tax(self):
        return self.price * self.invoice.tax_percent / 100

    @property
    def total(self):
        return self.price + self.tax

    @property
    def price(self):
        if self.unit == self.Units.QUANTITY:
            return self.unit_price * self.quantity
        elif self.unit == self.Units.PER_DAY:
            return self.unit_price * self.usage_days
        elif self.unit == self.Units.PER_HALF_MONTH:
            if self.start.day >= 15 or self.end.day <= 15:
                return self.unit_price
            else:
                return self.unit_price * 2

        return self.unit_price

    @property
    def usage_days(self):
        """
        Returns the number of days package was used from the time
        it was purchased or from the start of current month
        """
        full_days = utils.get_full_days(self.start, self.end)
        return full_days

    def terminate(self, end=None):
        self.freeze()
        self.end = end or timezone.now()
        self.save(update_fields=['end'])

    def name(self):
        raise NotImplementedError()

    def freeze(self):
        raise NotImplementedError()

    def __str__(self):
        return self.name


class GenericInvoiceItem(InvoiceItem):
    invoice = models.ForeignKey(Invoice, related_name='generic_items')
    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    quantity = models.PositiveIntegerField(default=0)

    scope = GenericForeignKey('content_type', 'object_id')
    details = JSONField(default={}, blank=True, help_text=_('Stores data about scope'))

    objects = managers.GenericInvoiceItemManager()
    tracker = FieldTracker()

    @property
    def name(self):
        if self.details:
            return self.details['name']
        return registrators.RegistrationManager.get_name(self.scope)

    def freeze(self):
        if self.scope:
            self.details = registrators.RegistrationManager.get_details(self.scope)
            self.details['name'] = registrators.RegistrationManager.get_name(self.scope)
            self.details['scope_uuid'] = self.scope.uuid.hex
            self.save(update_fields=['details'])


class OfferingItem(InvoiceItem):
    """ OfferingItem stores details for invoices about purchased custom offering item. """
    invoice = models.ForeignKey(Invoice, related_name='offering_items')
    offering = models.ForeignKey(support_models.Offering, on_delete=models.SET_NULL, null=True, related_name='+')
    offering_details = JSONField(default={}, blank=True, help_text=_('Stores data about offering'))
    tracker = FieldTracker()

    @property
    def name(self):
        if self.offering_details:
            return '%s (%s)' % (self.project_name, self.offering_details['offering_type'])

        return '%s (%s)' % (self.offering.project.name, self.offering.type)

    def freeze(self):
        """
        Saves offering type and project name in "offering_details" if offering exists
        """
        if self.offering:
            self.offering_details['offering_type'] = self.offering.type
            self.offering_details['offering_uuid'] = self.offering.uuid.hex
            self.save(update_fields=['offering_details'])

    def get_offering_uuid(self):
        if self.offering:
            return self.offering.uuid.hex
        else:
            return self.offering_details.get('offering_uuid')

    def get_offering_type(self):
        if self.offering:
            return self.offering.type
        else:
            return self.offering_details.get('offering_type')


class OpenStackItem(InvoiceItem):
    """ OpenStackItem stores details for invoices about purchased OpenStack packages """

    invoice = models.ForeignKey(Invoice, related_name='openstack_items')

    package = models.ForeignKey(package_models.OpenStackPackage, on_delete=models.SET_NULL, null=True, related_name='+')
    package_details = JSONField(default={}, blank=True, help_text=_('Stores data about package'))
    tracker = FieldTracker()

    @property
    def name(self):
        template_category = self.get_template_category()
        if template_category:
            return '%s (%s / %s)' % (self.get_tenant_name(), template_category, self.get_template_name())
        else:
            return '%s (%s)' % (self.get_tenant_name(), self.get_template_name())

    def freeze(self):
        """
        Saves tenant and package template names and uuids in "package_details" if package exists
        """
        if self.package:
            self.package_details['tenant_name'] = self.package.tenant.name
            self.package_details['tenant_uuid'] = self.package.tenant.uuid.hex
            self.package_details['template_name'] = self.package.template.name
            self.package_details['template_uuid'] = self.package.template.uuid.hex
            self.package_details['template_category'] = self.package.template.category
            self.save(update_fields=['package_details'])

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

    def get_tenant_name(self):
        if self.package:
            return self.package.tenant.name
        else:
            return self.package_details.get('tenant_name')

    def get_tenant_uuid(self):
        if self.package:
            return self.package.tenant.uuid.hex
        else:
            return self.package_details.get('tenant_uuid')

    def get_template_name(self):
        if self.package:
            return self.package.template.name
        else:
            return self.package_details.get('template_name')

    def get_template_uuid(self):
        if self.package:
            return self.package.template.uuid.hex
        else:
            return self.package_details.get('template_uuid')

    def get_template_category(self):
        if self.package:
            return self.package.template.get_category_display()
        else:
            template_category = self.package_details.get('template_category')
            if template_category:
                categories = dict(package_models.PackageTemplate.Categories.CHOICES)
                template_category = categories[template_category]

            return template_category


@python_2_unicode_compatible
class PaymentDetails(core_models.UuidMixin, models.Model):
    """ Customer payment details """

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        verbose_name = _('Payment details')
        verbose_name_plural = _('Payment details')

    customer = models.OneToOneField(structure_models.Customer, related_name='payment_details')
    accounting_start_date = models.DateTimeField(_('Start date of accounting'), default=timezone.now)
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

    def is_billable(self):
        return timezone.now() >= self.accounting_start_date

    def __str__(self):
        return 'PaymentDetails for %s' % self.customer
