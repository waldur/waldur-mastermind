from __future__ import unicode_literals

import six
from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField as BetterJSONField
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from django_fsm import transition, FSMIntegerField
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.core.fields import JSONField
from waldur_core.core.validators import FileTypeValidator
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.images import get_upload_path

from . import managers
from .attribute_types import ATTRIBUTE_TYPES
from .plugins import manager
from ..common import mixins as common_mixins


@python_2_unicode_compatible
class ServiceProvider(core_models.UuidMixin,
                      core_models.DescribableMixin,
                      structure_models.StructureModel,
                      structure_models.TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, related_name='+', on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        verbose_name = _('Service provider')

    def __str__(self):
        return six.text_type(self.customer)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-service-provider'


VectorizedImageValidator = FileTypeValidator(
    allowed_types=[
        'image/png',
        'image/jpeg',
        'image/svg+xml',
    ]
)


@python_2_unicode_compatible
class Category(core_models.UuidMixin,
               quotas_models.QuotaModelMixin,
               structure_models.TimeStampedModel):
    title = models.CharField(blank=False, max_length=255)
    icon = models.FileField(upload_to='marketplace_category_icons',
                            blank=True,
                            null=True,
                            validators=[VectorizedImageValidator])
    description = models.TextField(blank=True)

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        offering_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [Offering],
            path_to_scope='category',
        )

    class Meta(object):
        verbose_name = _('Category')
        verbose_name_plural = _('Categories')
        ordering = ('title',)

    def __str__(self):
        return six.text_type(self.title)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-category'


@python_2_unicode_compatible
class Section(structure_models.TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    category = models.ForeignKey(Category, related_name='sections')

    def __str__(self):
        return six.text_type(self.title)


@python_2_unicode_compatible
class Attribute(structure_models.TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    section = models.ForeignKey(Section, related_name='attributes')
    type = models.CharField(max_length=255, choices=ATTRIBUTE_TYPES)

    def __str__(self):
        return six.text_type(self.title)


@python_2_unicode_compatible
class AttributeOption(models.Model):
    attribute = models.ForeignKey(Attribute, related_name='options', on_delete=models.CASCADE)
    key = models.CharField(max_length=255)
    title = models.CharField(max_length=255)

    class Meta(object):
        unique_together = ('attribute', 'key')

    def __str__(self):
        return six.text_type(self.title)


@python_2_unicode_compatible
class Offering(core_models.UuidMixin,
               core_models.NameMixin,
               core_models.DescribableMixin,
               quotas_models.QuotaModelMixin,
               structure_models.StructureModel,
               structure_models.TimeStampedModel):
    thumbnail = models.ImageField(upload_to='marketplace_service_offering_thumbnails', blank=True, null=True)
    full_description = models.TextField(blank=True)
    rating = models.IntegerField(null=True,
                                 validators=[MaxValueValidator(5), MinValueValidator(1)],
                                 help_text=_('Rating is value from 1 to 5.'))
    category = models.ForeignKey(Category, related_name='offerings')
    customer = models.ForeignKey(structure_models.Customer, related_name='+', null=True)
    attributes = BetterJSONField(blank=True, default=dict)
    geolocations = JSONField(default=list, blank=True,
                             help_text=_('List of latitudes and longitudes. For example: '
                                         '[{"latitude": 123, "longitude": 345}, {"latitude": 456, "longitude": 678}]'))
    is_active = models.BooleanField(default=True)

    native_name = models.CharField(max_length=160, default='', blank=True)
    native_description = models.CharField(max_length=500, default='', blank=True)

    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')

    objects = managers.OfferingManager()

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        verbose_name = _('Offering')

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        order_item_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [OrderItem],
            path_to_scope='offering',
        )

    def __str__(self):
        return six.text_type(self.name)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering'


@python_2_unicode_compatible
class Screenshots(core_models.UuidMixin,
                  structure_models.StructureModel,
                  core_models.DescribableMixin,
                  structure_models.TimeStampedModel,
                  core_models.NameMixin):
    image = models.ImageField(upload_to=get_upload_path)
    thumbnail = models.ImageField(upload_to=get_upload_path, editable=False, null=True)
    offering = models.ForeignKey(Offering, related_name='screenshots')

    class Permissions(object):
        customer_path = 'offering__customer'

    class Meta(object):
        verbose_name = _('Screenshot')

    def __str__(self):
        return six.text_type(self.name)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-screenshot'


class Order(core_models.UuidMixin,
            structure_models.TimeStampedModel):
    class States(object):
        DRAFT = 1
        REQUESTED_FOR_APPROVAL = 2
        EXECUTING = 3
        DONE = 4
        TERMINATED = 5

        CHOICES = (
            (DRAFT, 'draft'),
            (REQUESTED_FOR_APPROVAL, 'requested for approval'),
            (EXECUTING, 'executing'),
            (DONE, 'done'),
            (TERMINATED, 'terminated'),
        )

    created_by = models.ForeignKey(core_models.User, related_name='orders')
    approved_by = models.ForeignKey(core_models.User, blank=True, null=True, related_name='+')
    approved_at = models.DateTimeField(editable=False, null=True, blank=True)
    project = models.ForeignKey(structure_models.Project)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    total_cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    tracker = FieldTracker()

    class Permissions(object):
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta(object):
        verbose_name = _('Order')
        ordering = ('created',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-order'

    @transition(field=state, source=States.DRAFT, target=States.REQUESTED_FOR_APPROVAL)
    def set_state_requested_for_approval(self):
        pass

    @transition(field=state, source=States.REQUESTED_FOR_APPROVAL, target=States.EXECUTING)
    def set_state_executing(self):
        pass

    @transition(field=state, source=States.EXECUTING, target=States.DONE)
    def set_state_done(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATED)
    def set_state_terminated(self):
        pass

    def get_approvers(self):
        User = get_user_model()
        users = []

        if settings.WALDUR_MARKETPLACE['NOTIFY_STAFF_ABOUT_APPROVALS']:
            users = User.objects.filter(is_staff=True, is_active=True)

        if settings.WALDUR_MARKETPLACE['OWNER_CAN_APPROVE_ORDER']:
            order_owners = self.project.customer.get_owners()
            users = order_owners if not users else users.union(order_owners)

        if settings.WALDUR_MARKETPLACE['MANAGER_CAN_APPROVE_ORDER']:
            order_managers = self.project.get_users(models.ProjectRole.MANAGER)
            users = order_managers if not users else users.union(order_managers)

        if settings.WALDUR_MARKETPLACE['ADMIN_CAN_APPROVE_ORDER']:
            order_admins = self.project.get_users(models.ProjectRole.ADMINISTRATOR)
            users = order_admins if not users else users.union(order_admins)

        return users and users.distinct()


class OrderItem(core_models.UuidMixin,
                structure_models.TimeStampedModel):
    order = models.ForeignKey(Order, related_name='items')
    offering = models.ForeignKey(Offering)
    attributes = BetterJSONField(blank=True, default=dict)
    cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    plan = models.ForeignKey('Plan', null=True, blank=True)

    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')
    objects = managers.OrderItemManager('scope')

    class Meta(object):
        verbose_name = _('Order item')
        ordering = ('created',)

    def process(self, user):
        manager.process(self, user)


class Plan(core_models.UuidMixin,
           structure_models.TimeStampedModel,
           core_models.NameMixin,
           core_models.DescribableMixin,
           common_mixins.UnitPriceMixin):
    offering = models.ForeignKey(Offering, related_name='plans')

    @classmethod
    def get_url_name(cls):
        return 'marketplace-plan'

    class Permissions(object):
        customer_path = 'offering__customer'
