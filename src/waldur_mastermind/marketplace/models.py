from __future__ import unicode_literals

import base64
from decimal import Decimal
import StringIO

from django.core.exceptions import ValidationError
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
from model_utils.models import TimeStampedModel
import six

from waldur_core.core import models as core_models, utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.core.validators import ImageValidator
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.images import get_upload_path

from . import managers
from .attribute_types import ATTRIBUTE_TYPES
from ..common import mixins as common_mixins


@python_2_unicode_compatible
class ServiceProvider(core_models.UuidMixin,
                      core_models.DescribableMixin,
                      structure_models.StructureModel,
                      TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)
    api_secret_code = models.CharField(max_length=255, null=True, blank=True)

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        verbose_name = _('Service provider')

    def __str__(self):
        return six.text_type(self.customer)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-service-provider'

    @property
    def has_active_offerings(self):
        return Offering.objects.filter(customer=self.customer).exclude(state=Offering.States.ARCHIVED).exists()

    def generate_api_secret_code(self):
        self.api_secret_code = core_utils.pwgen()

    def save(self, *args, **kwargs):
        if not self.pk:
            self.generate_api_secret_code()
        super(ServiceProvider, self).save(*args, **kwargs)


@python_2_unicode_compatible
class Category(core_models.UuidMixin,
               quotas_models.QuotaModelMixin,
               TimeStampedModel):
    title = models.CharField(blank=False, max_length=255)
    icon = models.FileField(upload_to='marketplace_category_icons',
                            blank=True,
                            null=True,
                            validators=[ImageValidator])
    description = models.TextField(blank=True)

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        offering_count = quotas_fields.QuotaField(is_backend=True)

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
class CategoryColumn(models.Model):
    """
    This model is needed in order to render resources table with extra columns.
    Usually each column corresponds to specific resource attribute.
    However, one table column may correspond to several resource attributes.
    In this case custom widget should be specified.
    If attribute field is specified, it is possible to filter and sort resources by it's value.
    """

    class Meta(object):
        ordering = ('category', 'index')

    category = models.ForeignKey(Category, related_name='columns')
    index = models.PositiveSmallIntegerField(help_text=_('Index allows to reorder columns.'))
    title = models.CharField(blank=False, max_length=255,
                             help_text=_('Title is rendered as column header.'))
    attribute = models.CharField(blank=True, max_length=255,
                                 help_text=_('Resource attribute is rendered as table cell.'))
    widget = models.CharField(blank=True, max_length=255,
                              help_text=_('Widget field allows to customise table cell rendering.'))

    def __str__(self):
        return six.text_type(self.title)

    def clean(self):
        if not self.attribute and not self.widget:
            raise ValidationError(_('Either attribute or widget field should be specified.'))


@python_2_unicode_compatible
class Section(TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    category = models.ForeignKey(Category, related_name='sections')
    is_standalone = models.BooleanField(
        default=False, help_text=_('Whether section is rendered as a separate tab.'))

    def __str__(self):
        return six.text_type(self.title)


@python_2_unicode_compatible
class Attribute(TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    section = models.ForeignKey(Section, related_name='attributes')
    type = models.CharField(max_length=255, choices=ATTRIBUTE_TYPES)
    required = models.BooleanField(default=False, help_text=_('A value must be provided for the attribute.'))

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


class ScopeMixin(models.Model):
    class Meta(object):
        abstract = True

    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')


@python_2_unicode_compatible
class Offering(core_models.UuidMixin,
               core_models.NameMixin,
               core_models.DescribableMixin,
               quotas_models.QuotaModelMixin,
               structure_models.StructureModel,
               TimeStampedModel,
               ScopeMixin):

    class States(object):
        DRAFT = 1
        ACTIVE = 2
        PAUSED = 3
        ARCHIVED = 4

        CHOICES = (
            (DRAFT, 'Draft'),
            (ACTIVE, 'Active'),
            (PAUSED, 'Paused'),
            (ARCHIVED, 'Archived'),
        )

    thumbnail = models.FileField(upload_to='marketplace_service_offering_thumbnails',
                                 blank=True,
                                 null=True,
                                 validators=[ImageValidator])
    full_description = models.TextField(blank=True)
    vendor_details = models.TextField(blank=True)
    rating = models.IntegerField(null=True,
                                 validators=[MaxValueValidator(5), MinValueValidator(1)],
                                 help_text=_('Rating is value from 1 to 5.'))
    category = models.ForeignKey(Category, related_name='offerings')
    customer = models.ForeignKey(structure_models.Customer, related_name='+', null=True)
    attributes = BetterJSONField(blank=True, default=dict, help_text=_('Fields describing Category.'))
    options = BetterJSONField(blank=True, default=dict, help_text=_('Fields describing Offering request form.'))
    geolocations = JSONField(default=list, blank=True,
                             help_text=_('List of latitudes and longitudes. For example: '
                                         '[{"latitude": 123, "longitude": 345}, {"latitude": 456, "longitude": 678}]'))

    native_name = models.CharField(max_length=160, default='', blank=True)
    native_description = models.CharField(max_length=500, default='', blank=True)

    type = models.CharField(max_length=100)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)

    # If offering is not shared, it is available only to following user categories:
    # 1) staff user;
    # 2) global support user;
    # 3) users with active permission in original customer;
    # 4) users with active permission in allowed customers and nested projects.
    shared = models.BooleanField(default=True, help_text=_('Anybody can use it'))
    allowed_customers = models.ManyToManyField(structure_models.Customer, blank=True)

    objects = managers.OfferingManager()
    tracker = FieldTracker()

    class Permissions(object):
        customer_path = 'customer'

    class Meta(object):
        verbose_name = _('Offering')

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        order_item_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [OrderItem],
            path_to_scope='offering',
        )

    @transition(field=state, source=[States.DRAFT, States.PAUSED], target=States.ACTIVE)
    def activate(self):
        pass

    @transition(field=state, source=States.ACTIVE, target=States.PAUSED)
    def pause(self):
        pass

    @transition(field=state, source='*', target=States.ARCHIVED)
    def archive(self):
        pass

    def __str__(self):
        return six.text_type(self.name)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering'

    def get_usage_components(self):
        components = self.components.filter(billing_type=OfferingComponent.BillingTypes.USAGE)
        return {component.type: component for component in components}


class OfferingComponent(core_models.DescribableMixin):
    class Meta(object):
        unique_together = ('type', 'offering')

    class BillingTypes(object):
        FIXED = 'fixed'
        USAGE = 'usage'

        CHOICES = (
            (FIXED, 'Fixed-price'),
            (USAGE, 'Usage-based'),
        )

    offering = models.ForeignKey(Offering, related_name='components')
    billing_type = models.CharField(choices=BillingTypes.CHOICES,
                                    default=BillingTypes.FIXED,
                                    max_length=5)
    type = models.CharField(max_length=50,
                            help_text=_('Unique internal name of the measured unit, for example floating_ip.'))
    name = models.CharField(max_length=150,
                            help_text=_('Display name for the measured unit, for example, Floating IP.'))
    measured_unit = models.CharField(max_length=30,
                                     help_text=_('Unit of measurement, for example, GB.'))


@python_2_unicode_compatible
class Plan(core_models.UuidMixin,
           TimeStampedModel,
           core_models.NameMixin,
           core_models.DescribableMixin,
           common_mixins.UnitPriceMixin,
           common_mixins.ProductCodeMixin,
           ScopeMixin):
    offering = models.ForeignKey(Offering, related_name='plans')
    archived = models.BooleanField(default=False, help_text=_('Forbids creation of new resources.'))
    objects = managers.MixinManager('scope')

    class Meta(object):
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-plan'

    class Permissions(object):
        customer_path = 'offering__customer'

    def get_estimate(self, limits=None):
        cost = self.unit_price

        if limits:
            components_map = self.offering.get_usage_components()
            component_prices = {c.component.type: c.price for c in self.components.all()}
            for key in components_map.keys():
                cost += component_prices.get(key, 0) * limits.get(key, 0)

        return cost

    def __str__(self):
        return self.name


class PlanComponent(models.Model):
    class Meta(object):
        unique_together = ('plan', 'component')

    PRICE_MAX_DIGITS = 14
    PRICE_DECIMAL_PLACES = 10

    plan = models.ForeignKey(Plan, related_name='components')
    component = models.ForeignKey(OfferingComponent, related_name='components', null=True)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0,
                                max_digits=PRICE_MAX_DIGITS,
                                decimal_places=PRICE_DECIMAL_PLACES,
                                validators=[MinValueValidator(Decimal('0'))],
                                verbose_name=_('Price per unit per billing period.'))


@python_2_unicode_compatible
class Screenshot(core_models.UuidMixin,
                 structure_models.StructureModel,
                 core_models.DescribableMixin,
                 TimeStampedModel,
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


class RequestTypeMixin(models.Model):
    class Types(object):
        CREATE = 1
        UPDATE = 2
        TERMINATE = 3

        CHOICES = (
            (CREATE, 'Create'),
            (UPDATE, 'Update'),
            (TERMINATE, 'Terminate'),
        )

    type = models.PositiveSmallIntegerField(choices=Types.CHOICES, default=Types.CREATE)

    class Meta(object):
        abstract = True


class CartItem(core_models.UuidMixin, TimeStampedModel, RequestTypeMixin):
    user = models.ForeignKey(core_models.User, related_name='+', on_delete=models.CASCADE)
    offering = models.ForeignKey(Offering, related_name='+', on_delete=models.CASCADE)
    plan = models.ForeignKey('Plan', null=True, blank=True)
    attributes = BetterJSONField(blank=True, default=dict)
    limits = BetterJSONField(blank=True, default=dict)

    class Meta(object):
        ordering = ('created',)

    @property
    def estimate(self):
        return self.plan.get_estimate(self.limits)


class Order(core_models.UuidMixin, TimeStampedModel):
    class States(object):
        REQUESTED_FOR_APPROVAL = 1
        EXECUTING = 2
        DONE = 3
        TERMINATED = 4

        CHOICES = (
            (REQUESTED_FOR_APPROVAL, 'requested for approval'),
            (EXECUTING, 'executing'),
            (DONE, 'done'),
            (TERMINATED, 'terminated'),
        )

    created_by = models.ForeignKey(core_models.User, related_name='orders')
    approved_by = models.ForeignKey(core_models.User, blank=True, null=True, related_name='+')
    approved_at = models.DateTimeField(editable=False, null=True, blank=True)
    project = models.ForeignKey(structure_models.Project)
    state = FSMIntegerField(default=States.REQUESTED_FOR_APPROVAL, choices=States.CHOICES)
    total_cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    tracker = FieldTracker()
    _file = models.TextField(blank=True, editable=False)

    class Permissions(object):
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta(object):
        verbose_name = _('Order')
        ordering = ('created',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-order'

    @transition(field=state, source=States.REQUESTED_FOR_APPROVAL, target=States.EXECUTING)
    def approve(self):
        pass

    @transition(field=state, source=States.EXECUTING, target=States.DONE)
    def complete(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATED)
    def terminate(self):
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
            order_managers = self.project.get_users(structure_models.ProjectRole.MANAGER)
            users = order_managers if not users else users.union(order_managers)

        if settings.WALDUR_MARKETPLACE['ADMIN_CAN_APPROVE_ORDER']:
            order_admins = self.project.get_users(structure_models.ProjectRole.ADMINISTRATOR)
            users = order_admins if not users else users.union(order_admins)

        return users and users.distinct()

    @property
    def file(self):
        if not self._file:
            return

        content = base64.b64decode(self._file)
        return StringIO.StringIO(content)

    @file.setter
    def file(self, value):
        self._file = value

    def has_file(self):
        return bool(self._file)

    def get_filename(self):
        return 'marketplace_order_{}.pdf'.format(self.uuid)

    def add_item(self, **kwargs):
        order_item = OrderItem(order=self, **kwargs)
        order_item.clean()
        order_item.init_cost()
        order_item.save()
        return order_item

    def init_total_cost(self):
        self.total_cost = sum(item.cost or 0 for item in self.items.all())


class Resource(core_models.UuidMixin, TimeStampedModel, ScopeMixin):
    """
    Core resource is abstract model, marketplace resource is not abstract,
    therefore we don't need to compromise database query efficiency when
    we are getting a list of all resources.

    While migration from ad-hoc resources to marketplace as single entry point is pending,
    the core resource model may continue to be used in plugins and referenced via
    generic foreign key, and marketplace resource is going to be used as consolidated
    model for synchronization with external plugins.

    Eventually it is expected that core resource model is going to be superseded by
    marketplace resource model as a primary mean.
    """
    class States(object):
        CREATING = 1
        OK = 2
        ERRED = 3
        UPDATING = 4
        TERMINATING = 5
        TERMINATED = 6

        CHOICES = (
            (CREATING, 'Creating'),
            (OK, 'OK'),
            (ERRED, 'Erred'),
            (UPDATING, 'Updating'),
            (TERMINATING, 'Terminating'),
            (TERMINATED, 'Terminated'),
        )

    class Permissions(object):
        customer_path = 'project__customer'
        project_path = 'project'

    state = FSMIntegerField(default=States.CREATING, choices=States.CHOICES)
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    offering = models.ForeignKey(Offering, related_name='+', on_delete=models.PROTECT)
    plan = models.ForeignKey(Plan, null=True, blank=True)
    attributes = BetterJSONField(blank=True, default=dict)
    limits = BetterJSONField(blank=True, default=dict)
    tracker = FieldTracker()
    objects = managers.MixinManager('scope')

    @property
    def name(self):
        return self.attributes.get('name')

    @transition(field=state, source=[States.CREATING, States.UPDATING], target=States.OK)
    def set_state_ok(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def set_state_erred(self):
        pass

    @transition(field=state, source='*', target=States.UPDATING)
    def set_state_updating(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATING)
    def set_state_terminating(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATED)
    def set_state_terminated(self):
        pass

    @property
    def backend_uuid(self):
        if self.scope:
            return self.scope.uuid

    @property
    def backend_type(self):
        if self.scope:
            return self.scope.get_scope_type()

    def init_quotas(self):
        if self.limits:
            components_map = self.offering.get_usage_components()
            for key, value in self.limits.items():
                component = components_map.get(key)
                if component:
                    ComponentQuota.objects.create(
                        resource=self,
                        component=component,
                        limit=value
                    )


class OrderItem(core_models.UuidMixin,
                core_models.ErrorMessageMixin,
                RequestTypeMixin,
                TimeStampedModel):
    class States(object):
        PENDING = 1
        EXECUTING = 2
        DONE = 3
        ERRED = 4

        CHOICES = (
            (PENDING, 'pending'),
            (EXECUTING, 'executing'),
            (DONE, 'done'),
            (ERRED, 'erred'),
        )

        TERMINAL_STATES = {DONE, ERRED}

    order = models.ForeignKey(Order, related_name='items')
    offering = models.ForeignKey(Offering)
    attributes = BetterJSONField(blank=True, default=dict)
    limits = BetterJSONField(blank=True, null=True, default=dict)
    cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    plan = models.ForeignKey('Plan', null=True, blank=True)
    resource = models.ForeignKey(Resource, null=True, blank=True)
    state = FSMIntegerField(default=States.PENDING, choices=States.CHOICES)
    tracker = FieldTracker()

    class Permissions(object):
        customer_path = 'order__project__customer'
        project_path = 'order__project'

    class Meta(object):
        verbose_name = _('Order item')
        ordering = ('created',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-order-item'

    @transition(field=state, source=[States.PENDING, States.ERRED], target=States.EXECUTING)
    def set_state_executing(self):
        pass

    @transition(field=state, source=States.EXECUTING, target=States.DONE)
    def set_state_done(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def set_state_erred(self):
        pass

    def clean(self):
        offering = self.offering
        customer = self.order.project.customer

        if offering.shared:
            return

        if offering.customer == customer:
            return

        if offering.allowed_customers.filter(pk=customer.pk).exists():
            return

        raise ValidationError(
            _('Offering "%s" is not allowed in organization "%s".') % (offering.name, customer.name)
        )

    def init_cost(self):
        if self.plan:
            self.cost = self.plan.get_estimate(self.limits)


class ComponentQuota(models.Model):
    resource = models.ForeignKey(Resource, related_name='quotas')
    component = models.ForeignKey(OfferingComponent,
                                  limit_choices_to={'billing_type': OfferingComponent.BillingTypes.USAGE})
    limit = models.PositiveIntegerField(default=-1)
    usage = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('resource', 'component')


class ComponentUsage(TimeStampedModel):
    resource = models.ForeignKey(Resource, related_name='usages')
    component = models.ForeignKey(OfferingComponent,
                                  limit_choices_to={'billing_type': OfferingComponent.BillingTypes.USAGE})
    usage = models.PositiveIntegerField(default=0)
    date = models.DateField()

    class Meta:
        unique_together = ('resource', 'component', 'date')


class ProjectResourceCount(models.Model):
    """
    This model allows to count current number of project resources by category.
    """
    project = models.ForeignKey(structure_models.Project, related_name='+')
    category = models.ForeignKey(Category, related_name='+')
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('project', 'category')
