from __future__ import unicode_literals

import base64
from decimal import Decimal
from HTMLParser import HTMLParser
import StringIO

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.template.loader import render_to_string
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
import pdfkit
import six

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins, utils as common_utils
from waldur_mastermind.support import models as support_models

from . import managers


@python_2_unicode_compatible
class ExpertProvider(core_models.UuidMixin,
                     TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, related_name='+', on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)

    class Meta(object):
        verbose_name = _('Expert providers')

    def __str__(self):
        return six.text_type(self.customer)

    @classmethod
    def get_url_name(cls):
        return 'expert-provider'


class PriceMixin(models.Model):
    class Meta(object):
        abstract = True

    price = models.DecimalField(max_digits=22, decimal_places=7,
                                validators=[MinValueValidator(Decimal('0'))],
                                default=0)


@python_2_unicode_compatible
class ExpertRequest(core_models.UuidMixin,
                    core_models.NameMixin,
                    PriceMixin,
                    common_mixins.ProductCodeMixin,
                    structure_models.StructureLoggableMixin,
                    TimeStampedModel):
    class States(object):
        PENDING = 'pending'
        ACTIVE = 'active'
        CANCELLED = 'cancelled'
        COMPLETED = 'completed'

        CHOICES = (
            (PENDING, _('Pending')),
            (ACTIVE, _('Active')),
            (CANCELLED, _('Cancelled')),
            (COMPLETED, _('Completed'))
        )

    description = models.TextField(blank=True)
    user = models.ForeignKey(core_models.User, related_name='+', on_delete=models.CASCADE,
                             help_text=_('The user which has created this request.'))
    project = models.ForeignKey(structure_models.Project, related_name='+', on_delete=models.SET_NULL, null=True)
    # Project name, project UUID, customer should be stored separately
    # because they are not available after project removal
    project_name = models.CharField(max_length=150, blank=True)
    project_uuid = models.CharField(max_length=32, blank=True)
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE, null=True)

    state = models.CharField(default=States.PENDING, max_length=30, choices=States.CHOICES)
    type = models.CharField(max_length=255)
    extra = core_fields.JSONField(default=dict)
    issue = models.ForeignKey(support_models.Issue, null=True, on_delete=models.SET_NULL)
    recurring_billing = models.BooleanField(
        default=False, help_text=_('Defines whether expert request has to be billed every month or only once'))
    objectives = models.TextField(blank=True)
    milestones = models.TextField(blank=True)
    contract_methodology = models.TextField(blank=True)
    out_of_scope = models.TextField(
        blank=True, help_text=_('Elements that are explicitly excluded from the contract'))
    common_tos = models.TextField(blank=True)

    tracker = FieldTracker()
    objects = managers.ExpertRequestManager()

    class Meta:
        ordering = ['-created']

    def get_log_fields(self):
        return super(ExpertRequest, self).get_log_fields() + ('state', 'project', 'user')

    @classmethod
    def get_url_name(cls):
        return 'expert-request'

    @property
    def type_label(self):
        offerings = settings.WALDUR_SUPPORT.get('CONTRACT', {}).get('offerings', {})
        type_settings = offerings.get(self.type, {})
        return type_settings.get('label', None)

    @property
    def planned_budget(self):
        price = self.extra.get('price')
        if price:
            try:
                return float(price)
            except ValueError:
                return 0
        return 0

    def __str__(self):
        if self.project:
            return '%s (%s)' % (self.name, self.project)
        return self.name


class ExpertBid(core_models.UuidMixin,
                core_models.DescribableMixin,
                PriceMixin,
                structure_models.StructureLoggableMixin,
                TimeStampedModel):
    user = models.ForeignKey(core_models.User, related_name='+', on_delete=models.CASCADE,
                             help_text=_('The user which has created this bid.'))
    request = models.ForeignKey(ExpertRequest, on_delete=models.CASCADE, related_name='bids')
    team = models.ForeignKey(structure_models.Project)
    objects = managers.ExpertBidManager()
    tracker = FieldTracker()

    class Meta:
        ordering = ['-created']

    class Permissions(object):
        customer_path = 'request__project__customer'

    def get_log_fields(self):
        return super(ExpertBid, self).get_log_fields() + ('request', 'user', 'price')

    @classmethod
    def get_url_name(cls):
        return 'expert-bid'


class ExpertContract(PriceMixin, core_models.DescribableMixin, TimeStampedModel):
    request = models.OneToOneField(ExpertRequest, on_delete=models.CASCADE, related_name='contract')
    team = models.ForeignKey(structure_models.Project, related_name='+', on_delete=models.SET_NULL, null=True)

    # Team name, team UUID and customer should be stored separately
    # because they are not available after project removal
    team_name = models.CharField(max_length=150, blank=True)
    team_uuid = models.CharField(max_length=32, blank=True)
    team_customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE, null=True)

    _file = models.TextField(blank=True, editable=False)

    def get_file(self):
        if not self._file:
            return

        content = base64.b64decode(self._file)
        s = StringIO.StringIO(content)
        return s

    def create_file(self):
        parser = HTMLParser()
        context = {
            'contract': self,
            'client': {
                'name': self.team.customer.name,
                'laws': self.team.customer.country,
                'number': self.team.customer.registration_code,
                'representative': self.team.customer.get_owners()[0].full_name
            },
            'expert': {
                'name': self.request.project.customer.name,
                'laws': self.request.project.customer.country,
                'number': self.request.project.customer.registration_code,
                'representative': self.request.project.customer.get_owners()[0].full_name
            },
            'service': {
                'description': parser.unescape(self.request.description),
                'date_end': ''
            },
            'status': ExpertRequest.States.COMPLETED,
            'remuneration': {
                'type': 'regular payment' if self.request.recurring_billing else 'one-time payment',
                'value': common_utils.quantize_price(self.price),
                'currency': settings.WALDUR_CORE['CURRENCY_NAME']
            },
            'period': ''
        }
        contract_text = render_to_string('experts/contract_template.html', context)
        pdf = pdfkit.from_string(contract_text, False)
        self._file = base64.b64encode(pdf)
        self.save()

    def has_file(self):
        return bool(self._file)

    def get_filename(self):
        filename = "{year}_{month:02d}_{day:02d}_expert_contract_{id}.pdf".format(
            id=self.id,
            year=self.created.year,
            month=self.created.month,
            day=self.created.day
        )
        return filename

    class Meta:
        ordering = ['-created']
