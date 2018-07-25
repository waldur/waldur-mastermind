from __future__ import unicode_literals

import logging

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django_fsm import transition, FSMIntegerField
from django.utils.translation import ugettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core.fields import JSONField
from waldur_core.core.models import UuidMixin, ErrorMessageMixin, BackendModelMixin
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.structure.models import Customer


from . import backend

logger = logging.getLogger(__name__)


@python_2_unicode_compatible
class Payment(LoggableMixin, TimeStampedModel, UuidMixin, ErrorMessageMixin):
    class Meta(object):
        ordering = ['-modified']

    class Permissions(object):
        customer_path = 'customer'

    class States(object):
        INIT = 0
        CREATED = 1
        APPROVED = 2
        CANCELLED = 3
        ERRED = 4

    STATE_CHOICES = (
        (States.INIT, 'Initial'),
        (States.CREATED, 'Created'),
        (States.APPROVED, 'Approved'),
        (States.ERRED, 'Erred'),
    )

    state = FSMIntegerField(default=States.INIT, choices=STATE_CHOICES)

    customer = models.ForeignKey(Customer)
    amount = models.DecimalField(max_digits=9, decimal_places=2)
    tax = models.DecimalField(max_digits=9, decimal_places=2, default=0)

    # Payment ID is persistent identifier of payment
    backend_id = models.CharField(max_length=255, null=True)

    # Token is temporary identifier of payment
    token = models.CharField(max_length=255, null=True)

    # URL is fetched from backend
    approval_url = models.URLField()

    def __str__(self):
        return "%s %.2f %s" % (self.modified, self.amount, self.customer.name)

    def get_backend(self):
        return backend.PaypalBackend()

    @classmethod
    def get_url_name(cls):
        return 'paypal-payment'

    def get_log_fields(self):
        return ('uuid', 'customer', 'amount', 'modified', 'status')

    @transition(field=state, source=States.INIT, target=States.CREATED)
    def set_created(self):
        pass

    @transition(field=state, source=States.CREATED, target=States.APPROVED)
    def set_approved(self):
        pass

    @transition(field=state, source=States.CREATED, target=States.CANCELLED)
    def set_cancelled(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def set_erred(self):
        pass


@python_2_unicode_compatible
class Invoice(LoggableMixin, UuidMixin, BackendModelMixin):
    class Meta(object):
        ordering = ['-invoice_date']

    class Permissions(object):
        customer_path = 'customer'

    class States(object):
        DRAFT = 'DRAFT'
        SENT = 'SENT'
        PAID = 'PAID'
        MARKED_AS_PAID = 'MARKED_AS_PAID'
        CANCELLED = 'CANCELLED'
        REFUNDED = 'REFUNDED'
        PARTIALLY_REFUNDED = 'PARTIALLY_REFUNDED'
        MARKED_AS_REFUNDED = 'MARKED_AS_REFUNDED'
        UNPAID = 'UNPAID'
        PAYMENT_PENDING = 'PAYMENT_PENDING'

        CHOICES = ((DRAFT, _('Draft')), (SENT, _('Sent')), (PAID, _('Paid')), (MARKED_AS_PAID, _('Marked as paid')),
                   (CANCELLED, _('Cancelled')), (REFUNDED, _('Refunded')),
                   (PARTIALLY_REFUNDED, _('Partially refunded')), (MARKED_AS_REFUNDED, _('Marked as refunded')),
                   (UNPAID, _('Unpaid')), (PAYMENT_PENDING, _('Payment pending')))

    customer = models.ForeignKey(Customer, related_name='paypal_invoices')
    state = models.CharField(max_length=30, choices=States.CHOICES, default=States.DRAFT)
    invoice_date = models.DateField()
    end_date = models.DateField()
    pdf = models.FileField(upload_to='paypal-invoices', blank=True, null=True)
    number = models.CharField(max_length=30)
    tax_percent = models.DecimalField(default=0, max_digits=4, decimal_places=2,
                                      validators=[MinValueValidator(0), MaxValueValidator(100)])
    backend_id = models.CharField(max_length=128, blank=True)
    issuer_details = JSONField(default=dict, blank=True, help_text=_('Stores data about invoice issuer'))
    payment_details = JSONField(default=dict, blank=True, help_text=_('Stores data about customer payment details'))
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    year = models.PositiveSmallIntegerField()

    def get_backend(self):
        return backend.PaypalBackend()

    @classmethod
    def get_backend_fields(cls):
        return super(Invoice, cls).get_backend_fields() + (
            'state', 'issuer_details', 'number', 'payment_details', 'backend_id')

    @classmethod
    def get_url_name(cls):
        return 'paypal-invoice'

    @property
    def file_name(self):
        return '{}-invoice-{}.pdf'.format(self.invoice_date.strftime('%Y-%m-%d'), self.pk)

    @property
    def total(self):
        return self.price + self.tax

    @property
    def price(self):
        return sum(item.price for item in self.items.all())

    @property
    def tax(self):
        return self.price * self.tax_percent / 100

    def get_log_fields(self):
        return ('uuid', 'customer', 'total', 'invoice_date', 'end_date')

    def __str__(self):
        return "Invoice #%s" % self.number or self.id


class InvoiceItem(models.Model):
    class Meta(object):
        ordering = ['invoice', '-start']

    class UnitsOfMeasure(object):
        QUANTITY = 'QUANTITY'
        HOURS = 'HOURS'
        AMOUNT = 'AMOUNT'

        CHOICES = ((QUANTITY, _('Quantity')), (HOURS, _('Hours')), (AMOUNT, _('Amount')))

    invoice = models.ForeignKey(Invoice, related_name='items')
    price = models.DecimalField(max_digits=9, decimal_places=2)
    tax = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=9, decimal_places=2)
    quantity = models.PositiveIntegerField(default=0)
    unit_of_measure = models.CharField(max_length=30, choices=UnitsOfMeasure.CHOICES, default=UnitsOfMeasure.HOURS)
    name = models.CharField(max_length=255)
    start = models.DateTimeField(null=True)
    end = models.DateTimeField(null=True)
