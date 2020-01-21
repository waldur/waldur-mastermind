import base64
from decimal import Decimal
from io import BytesIO

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField as BetterJSONField
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django_fsm import transition, FSMIntegerField
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel, TimeFramedModel
from rest_framework import exceptions as rf_exceptions

from waldur_core.core import models as core_models, utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.media.validators import ImageValidator
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure import models as structure_models
from waldur_core.media.models import get_upload_path

from . import managers, plugins
from .attribute_types import ATTRIBUTE_TYPES
from ..common import mixins as common_mixins


class ServiceProvider(core_models.UuidMixin,
                      core_models.DescribableMixin,
                      structure_models.StructureModel,
                      TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)
    api_secret_code = models.CharField(max_length=255, null=True, blank=True)
    lead_email = models.EmailField(null=True, blank=True,
                                   help_text=_('Email for notification about new request based order items. '
                                               'If this field is set, notifications will be sent.'))
    lead_subject = models.CharField(max_length=255, blank=True,
                                    help_text=_('Notification subject template. '
                                                'Django template variables can be used.'))
    lead_body = models.TextField(blank=True,
                                 help_text=_('Notification body template. '
                                             'Django template variables can be used.'))

    class Permissions:
        customer_path = 'customer'

    class Meta:
        verbose_name = _('Service provider')

    def __str__(self):
        return str(self.customer)

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


class Category(core_models.UuidMixin,
               quotas_models.QuotaModelMixin,
               TimeStampedModel):
    title = models.CharField(blank=False, max_length=255)
    icon = models.FileField(upload_to='marketplace_category_icons',
                            blank=True,
                            null=True,
                            validators=[ImageValidator])
    description = models.TextField(blank=True)
    backend_id = models.CharField(max_length=255, blank=True)

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        offering_count = quotas_fields.QuotaField(is_backend=True)

    class Meta:
        verbose_name = _('Category')
        verbose_name_plural = _('Categories')
        ordering = ('title',)

    def __str__(self):
        return str(self.title)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-category'


class CategoryColumn(models.Model):
    """
    This model is needed in order to render resources table with extra columns.
    Usually each column corresponds to specific resource attribute.
    However, one table column may correspond to several resource attributes.
    In this case custom widget should be specified.
    If attribute field is specified, it is possible to filter and sort resources by it's value.
    """

    class Meta:
        ordering = ('category', 'index')

    category = models.ForeignKey(on_delete=models.CASCADE, to=Category, related_name='columns')
    index = models.PositiveSmallIntegerField(help_text=_('Index allows to reorder columns.'))
    title = models.CharField(blank=False, max_length=255,
                             help_text=_('Title is rendered as column header.'))
    attribute = models.CharField(blank=True, max_length=255,
                                 help_text=_('Resource attribute is rendered as table cell.'))
    widget = models.CharField(blank=True, null=True, max_length=255,
                              help_text=_('Widget field allows to customise table cell rendering.'))

    def __str__(self):
        return str(self.title)

    def clean(self):
        if not self.attribute and not self.widget:
            raise ValidationError(_('Either attribute or widget field should be specified.'))


class Section(TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    category = models.ForeignKey(on_delete=models.CASCADE, to=Category, related_name='sections')
    is_standalone = models.BooleanField(
        default=False, help_text=_('Whether section is rendered as a separate tab.'))

    def __str__(self):
        return str(self.title)


InternalNameValidator = RegexValidator('^[a-zA-Z0-9_]+$')


class Attribute(TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255, validators=[InternalNameValidator])
    title = models.CharField(blank=False, max_length=255)
    section = models.ForeignKey(on_delete=models.CASCADE, to=Section, related_name='attributes')
    type = models.CharField(max_length=255, choices=ATTRIBUTE_TYPES)
    required = models.BooleanField(default=False, help_text=_('A value must be provided for the attribute.'))
    default = BetterJSONField(null=True, blank=True)

    def __str__(self):
        return str(self.title)


class AttributeOption(models.Model):
    attribute = models.ForeignKey(Attribute, related_name='options', on_delete=models.CASCADE)
    key = models.CharField(max_length=255, validators=[InternalNameValidator])
    title = models.CharField(max_length=255)

    class Meta:
        unique_together = ('attribute', 'key')

    def __str__(self):
        return str(self.title)


class ScopeMixin(models.Model):
    class Meta:
        abstract = True

    content_type = models.ForeignKey(
        to=ContentType, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True, blank=True)
    scope = GenericForeignKey('content_type', 'object_id')


class BaseComponent(core_models.DescribableMixin):
    class Meta:
        abstract = True

    name = models.CharField(max_length=150,
                            help_text=_('Display name for the measured unit, for example, Floating IP.'))
    type = models.CharField(max_length=50,
                            help_text=_('Unique internal name of the measured unit, for example floating_ip.'),
                            validators=[InternalNameValidator])
    measured_unit = models.CharField(max_length=30,
                                     help_text=_('Unit of measurement, for example, GB.'),
                                     blank=True)


class CategoryComponent(BaseComponent):
    class Meta:
        unique_together = ('type', 'category')

    category = models.ForeignKey(on_delete=models.CASCADE, to=Category, related_name='components')

    def __str__(self):
        return '%s, category: %s' % (self.name, self.category.title)


class CategoryComponentUsage(ScopeMixin):
    component = models.ForeignKey(on_delete=models.CASCADE, to=CategoryComponent)
    date = models.DateField()
    reported_usage = models.PositiveIntegerField(null=True)
    fixed_usage = models.PositiveIntegerField(null=True)
    objects = managers.MixinManager('scope')

    def __str__(self):
        return 'component: %s, date: %s' % (str(self.component.name), self.date)


class Offering(core_models.UuidMixin,
               core_models.NameMixin,
               core_models.DescribableMixin,
               quotas_models.QuotaModelMixin,
               structure_models.StructureModel,
               TimeStampedModel,
               ScopeMixin,
               LoggableMixin):

    class States:
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
    category = models.ForeignKey(on_delete=models.CASCADE, to=Category, related_name='offerings')
    customer = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.Customer, related_name='+', null=True)
    # Volume offering is linked with VPC offering via parent field
    parent = models.ForeignKey(on_delete=models.CASCADE, to='Offering', null=True, blank=True)
    attributes = BetterJSONField(blank=True, default=dict, help_text=_('Fields describing Category.'))
    options = BetterJSONField(blank=True, default=dict, help_text=_('Fields describing Offering request form.'))
    plugin_options = BetterJSONField(blank=True, default=dict,
                                     help_text=_('Data used by specific plugin, such as credentials and hooks.'))
    geolocations = JSONField(default=list, blank=True,
                             help_text=_('List of latitudes and longitudes. For example: '
                                         '[{"latitude": 123, "longitude": 345}, {"latitude": 456, "longitude": 678}]'))

    native_name = models.CharField(max_length=160, default='', blank=True)
    native_description = models.CharField(max_length=500, default='', blank=True)
    terms_of_service = models.TextField(blank=True)

    type = models.CharField(max_length=100)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    paused_reason = models.TextField(blank=True)

    # If offering is not shared, it is available only to following user categories:
    # 1) staff user;
    # 2) global support user;
    # 3) users with active permission in original customer;
    # 4) users with active permission in allowed customers and nested projects.
    shared = models.BooleanField(default=True, help_text=_('Accessible to all customers.'))
    allowed_customers = models.ManyToManyField(structure_models.Customer, blank=True)

    billable = models.BooleanField(default=True, help_text=_('Purchase and usage is invoiced.'))
    backend_id = models.CharField(max_length=255, blank=True)

    objects = managers.OfferingManager()
    tracker = FieldTracker()

    class Permissions:
        customer_path = 'customer'

    class Meta:
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
        return str(self.name)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering'

    def get_usage_components(self):
        components = self.components.filter(billing_type=OfferingComponent.BillingTypes.USAGE)
        return {component.type: component for component in components}

    @cached_property
    def is_usage_based(self):
        return self.components.filter(
            billing_type=OfferingComponent.BillingTypes.USAGE,
            use_limit_for_billing=False,
        ).exists()

    @property
    def is_private(self):
        return not self.billable and not self.shared


class OfferingComponent(common_mixins.ProductCodeMixin, BaseComponent, ScopeMixin):
    class Meta:
        unique_together = ('type', 'offering')

    class BillingTypes:
        FIXED = 'fixed'
        USAGE = 'usage'
        ONE_TIME = 'one'
        ON_PLAN_SWITCH = 'few'

        CHOICES = (
            (FIXED, 'Fixed-price'),
            (USAGE, 'Usage-based'),
            (ONE_TIME, 'One-time'),
            # applies fee on resource activation and every time a plan has changed, using pricing of a new plan
            (ON_PLAN_SWITCH, 'One-time on plan switch'),
        )

    class LimitPeriods:
        MONTH = 'month'
        TOTAL = 'total'

        CHOICES = (
            (MONTH, 'Maximum monthly - every month service provider '
                    'can report up to the amount requested by user.'),
            (TOTAL, 'Maximum total - SP can report up to the requested '
                    'amount over the whole active state of resource.'),
        )

    offering = models.ForeignKey(on_delete=models.CASCADE, to=Offering, related_name='components')
    parent = models.ForeignKey(on_delete=models.CASCADE, to=CategoryComponent, null=True, blank=True)
    billing_type = models.CharField(choices=BillingTypes.CHOICES,
                                    default=BillingTypes.FIXED,
                                    max_length=5)
    limit_period = models.CharField(choices=LimitPeriods.CHOICES,
                                    blank=True,
                                    null=True,
                                    max_length=5)
    limit_amount = models.IntegerField(blank=True, null=True)
    max_value = models.IntegerField(blank=True, null=True)
    min_value = models.IntegerField(blank=True, null=True)
    disable_quotas = models.BooleanField(
        default=False,
        help_text=_('Do not allow user to specify quotas when offering is provisioned.')
    )
    use_limit_for_billing = models.BooleanField(
        default=False,
        help_text=_('Charge for usage-based component is based on user-requested limit.')
    )
    objects = managers.MixinManager('scope')

    def validate_amount(self, resource, amount, date):
        if not self.limit_period or not self.limit_amount:
            return

        usages = ComponentUsage.objects.filter(resource=resource, component=self)

        if self.limit_period == OfferingComponent.LimitPeriods.MONTH:
            usages = usages.filter(date=core_utils.month_start(date))

        total = usages.aggregate(models.Sum('usage'))['usage__sum'] or 0

        if total + amount > self.limit_amount:
            raise rf_exceptions.ValidationError(
                _('Total amount exceeds exceeds limit. Total amount: %s, limit: %s.') % (
                    total + amount, self.limit_amount)
            )

    def __str__(self):
        return str(self.name)


class Plan(core_models.UuidMixin,
           TimeStampedModel,
           core_models.NameMixin,
           core_models.DescribableMixin,
           common_mixins.UnitPriceMixin,
           common_mixins.ProductCodeMixin,
           ScopeMixin,
           LoggableMixin):
    """
    Plan unit price is computed as a sum of its fixed components'
    price multiplied by component amount when offering with plans
    is created via REST API. Usage-based components don't contribute to plan price.
    It is assumed that plan price is updated manually when plan component is managed via Django ORM.
    """
    offering = models.ForeignKey(on_delete=models.CASCADE, to=Offering, related_name='plans')
    archived = models.BooleanField(default=False, help_text=_('Forbids creation of new resources.'))
    objects = managers.MixinManager('scope')
    backend_id = models.CharField(max_length=255, blank=True)
    max_amount = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1)],
        help_text=_('Maximum number of plans that could be active. '
                    'Plan is disabled when maximum amount is reached.')
    )
    tracker = FieldTracker()

    class Meta:
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-plan'

    class Permissions:
        customer_path = 'offering__customer'

    def get_estimate(self, limits=None):
        cost = self.unit_price

        if limits:
            available_limits = plugins.manager.get_available_limits(self.offering.type)
            components = self.offering.components.filter(
                Q(billing_type=OfferingComponent.BillingTypes.USAGE) |
                Q(type__in=available_limits)
            )
            components_map = {component.type: component for component in components}
            component_prices = {c.component.type: c.price for c in self.components.all()}
            builtin_components = plugins.manager.get_components(self.offering.type)
            component_factors = {c.type: c.factor for c in builtin_components}
            for key in components_map.keys():
                price = component_prices.get(key, 0)
                limit = limits.get(key, 0)
                factor = component_factors.get(key, 1)
                cost += Decimal(price) * limit / factor

        return cost

    def __str__(self):
        return str(self.name)

    @property
    def init_price(self):
        return self.sum_components(OfferingComponent.BillingTypes.ONE_TIME)

    @property
    def switch_price(self):
        return self.sum_components(OfferingComponent.BillingTypes.ON_PLAN_SWITCH)

    def sum_components(self, billing_type):
        components = self.components.filter(component__billing_type=billing_type)
        return components.aggregate(sum=models.Sum('price'))['sum'] or 0

    @property
    def has_connected_resources(self):
        return Resource.objects.filter(plan=self).exists()

    @property
    def is_active(self):
        if not self.max_amount:
            return True
        usage = Resource.objects.filter(plan=self).exclude(state=Resource.States.TERMINATED).count()
        return self.max_amount > usage


class PlanComponent(models.Model):
    class Meta:
        unique_together = ('plan', 'component')

    PRICE_MAX_DIGITS = 15
    PRICE_DECIMAL_PLACES = 7

    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan, related_name='components')
    component = models.ForeignKey(on_delete=models.CASCADE, to=OfferingComponent, related_name='components', null=True)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0,
                                max_digits=PRICE_MAX_DIGITS,
                                decimal_places=PRICE_DECIMAL_PLACES,
                                validators=[MinValueValidator(Decimal('0'))],
                                verbose_name=_('Price per unit per billing period.'))
    tracker = FieldTracker()

    @property
    def has_connected_resources(self):
        return self.plan.has_connected_resources

    def __str__(self):
        name = self.component and self.component.name
        if name:
            return '%s, plan: %s' % (name, self.plan.name)
        return 'for plan: %s' % self.plan.name


class Screenshot(core_models.UuidMixin,
                 structure_models.StructureModel,
                 core_models.DescribableMixin,
                 TimeStampedModel,
                 core_models.NameMixin):
    image = models.ImageField(upload_to=get_upload_path)
    thumbnail = models.ImageField(upload_to=get_upload_path, editable=False, null=True)
    offering = models.ForeignKey(on_delete=models.CASCADE, to=Offering, related_name='screenshots')

    class Permissions:
        customer_path = 'offering__customer'

    class Meta:
        verbose_name = _('Screenshot')

    def __str__(self):
        return str(self.name)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-screenshot'


class CostEstimateMixin(models.Model):
    class Meta:
        abstract = True

    # Cost estimate is computed with respect to fixed plan components and usage-based limits
    cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan, null=True, blank=True)
    limits = BetterJSONField(blank=True, default=dict)

    def init_cost(self):
        if self.plan:
            self.cost = self.plan.get_estimate(self.limits)


class RequestTypeMixin(CostEstimateMixin):
    class Types:
        CREATE = 1
        UPDATE = 2
        TERMINATE = 3

        CHOICES = (
            (CREATE, 'Create'),
            (UPDATE, 'Update'),
            (TERMINATE, 'Terminate'),
        )

    type = models.PositiveSmallIntegerField(choices=Types.CHOICES, default=Types.CREATE)

    class Meta:
        abstract = True

    def init_cost(self):
        super(RequestTypeMixin, self).init_cost()
        if self.plan:
            if self.type == RequestTypeMixin.Types.CREATE:
                self.cost += self.plan.init_price
            elif self.type == RequestTypeMixin.Types.UPDATE:
                self.cost += self.plan.switch_price


class CartItem(core_models.UuidMixin, TimeStampedModel, RequestTypeMixin):
    user = models.ForeignKey(core_models.User, related_name='+', on_delete=models.CASCADE)
    project = models.ForeignKey(structure_models.Project, related_name='+', on_delete=models.CASCADE)
    offering = models.ForeignKey(Offering, related_name='+', on_delete=models.CASCADE)
    attributes = BetterJSONField(blank=True, default=dict)

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta:
        ordering = ('created',)

    def __str__(self):
        return 'user: %s, offering: %s' % (self.user.username, self.offering.name)


class Order(core_models.UuidMixin, TimeStampedModel, LoggableMixin):
    class States:
        REQUESTED_FOR_APPROVAL = 1
        EXECUTING = 2
        DONE = 3
        TERMINATED = 4
        ERRED = 5
        REJECTED = 6

        CHOICES = (
            (REQUESTED_FOR_APPROVAL, 'requested for approval'),
            (EXECUTING, 'executing'),
            (DONE, 'done'),
            (TERMINATED, 'terminated'),
            (ERRED, 'erred'),
            (REJECTED, 'rejected'),
        )

    created_by = models.ForeignKey(on_delete=models.CASCADE, to=core_models.User, related_name='orders')
    approved_by = models.ForeignKey(on_delete=models.CASCADE, to=core_models.User, blank=True, null=True, related_name='+')
    approved_at = models.DateTimeField(editable=False, null=True, blank=True)
    project = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.Project)
    state = FSMIntegerField(default=States.REQUESTED_FOR_APPROVAL, choices=States.CHOICES)
    total_cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    tracker = FieldTracker()
    _file = models.TextField(blank=True, editable=False)

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta:
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

    @transition(field=state, source=States.REQUESTED_FOR_APPROVAL, target=States.REJECTED)
    def reject(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def fail(self):
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
        return BytesIO(content)

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

    def get_log_fields(self):
        return (
            'uuid', 'name', 'created', 'modified',
            'created_by', 'approved_by', 'approved_at',
            'project', 'get_state_display', 'total_cost',
        )

    def _get_log_context(self, entity_name):
        context = super(Order, self)._get_log_context(entity_name)
        context['order_items'] = [item._get_log_context('') for item in self.items.all()]
        return context

    def __str__(self):
        return 'project: %s, created by: %s' % (self.project.name, self.created_by.username)


class Resource(CostEstimateMixin,
               core_models.UuidMixin,
               TimeStampedModel,
               ScopeMixin,
               structure_models.StructureLoggableMixin,
               core_models.NameMixin):
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
    class States:
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

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    state = FSMIntegerField(default=States.CREATING, choices=States.CHOICES)
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    offering = models.ForeignKey(Offering, related_name='+', on_delete=models.PROTECT)
    attributes = BetterJSONField(blank=True, default=dict)
    backend_metadata = BetterJSONField(blank=True, default=dict)
    current_usages = BetterJSONField(blank=True, default=dict)
    tracker = FieldTracker()
    objects = managers.MixinManager('scope')

    @property
    def customer(self):
        return self.project.customer

    @transition(field=state, source=[States.ERRED, States.CREATING, States.UPDATING], target=States.OK)
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

    def get_log_fields(self):
        return (
            'uuid', 'name', 'project', 'offering', 'created', 'modified',
            'attributes', 'cost', 'plan', 'limits', 'get_state_display',
            'backend_metadata', 'backend_uuid', 'backend_type',
        )

    def __str__(self):
        return str(self.name)


class ResourcePlanPeriod(TimeStampedModel, TimeFramedModel, core_models.UuidMixin):
    """
    This model allows to track billing plan for timeframes during resource lifecycle.
    """
    resource = models.ForeignKey(on_delete=models.CASCADE, to=Resource, related_name='+')
    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan)

    def __str__(self):
        return str(self.resource.name)

    @property
    def current_components(self):
        now = timezone.now()
        return self.components.filter(billing_period=core_utils.month_start(now))


class OrderItem(core_models.UuidMixin,
                core_models.ErrorMessageMixin,
                RequestTypeMixin,
                structure_models.StructureLoggableMixin,
                TimeStampedModel):
    class States:
        PENDING = 1
        EXECUTING = 2
        DONE = 3
        ERRED = 4
        TERMINATED = 5
        TERMINATING = 6

        CHOICES = (
            (PENDING, 'pending'),
            (EXECUTING, 'executing'),
            (DONE, 'done'),
            (ERRED, 'erred'),
            (TERMINATED, 'terminated'),
            (TERMINATING, 'terminating'),
        )

        TERMINAL_STATES = {DONE, ERRED}

    order = models.ForeignKey(on_delete=models.CASCADE, to=Order, related_name='items')
    offering = models.ForeignKey(on_delete=models.CASCADE, to=Offering)
    attributes = BetterJSONField(blank=True, default=dict)
    old_plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan, related_name='+', null=True, blank=True)
    resource = models.ForeignKey(on_delete=models.CASCADE, to=Resource, null=True, blank=True)
    state = FSMIntegerField(default=States.PENDING, choices=States.CHOICES)
    activated = models.DateTimeField(_('activation date'), null=True, blank=True)
    output = models.TextField(blank=True)
    tracker = FieldTracker()

    class Permissions:
        customer_path = 'order__project__customer'
        project_path = 'order__project'

    class Meta:
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
        self.activated = timezone.now()
        self.save()

    @transition(field=state, source='*', target=States.ERRED)
    def set_state_erred(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATED)
    def set_state_terminated(self):
        pass

    @transition(field=state, source=[States.PENDING, States.EXECUTING], target=States.TERMINATING)
    def set_state_terminating(self):
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

    def get_log_fields(self):
        return (
            'uuid', 'created', 'modified',
            'cost', 'limits', 'attributes',
            'offering', 'resource', 'plan',
            'get_state_display', 'get_type_display',
        )

    @property
    def safe_attributes(self):
        """
        Get attributes excluding secret attributes, such as username and password.
        """
        secret_attributes = plugins.manager.get_secret_attributes(self.offering.type)
        attributes = self.attributes or {}
        return {
            key: attributes[key]
            for key in attributes.keys()
            if key not in secret_attributes
        }

    def __str__(self):
        return 'type: %s, created_by: %s' % (self.get_type_display(), self.order.created_by)


class ComponentQuota(models.Model):
    resource = models.ForeignKey(on_delete=models.CASCADE, to=Resource, related_name='quotas')
    component = models.ForeignKey(on_delete=models.CASCADE, to=OfferingComponent,
                                  limit_choices_to={'billing_type': OfferingComponent.BillingTypes.USAGE})
    limit = models.PositiveIntegerField(default=-1)
    usage = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('resource', 'component')

    def __str__(self):
        return 'resource: %s, component: %s' % (self.resource.name, self.component.name)


class ComponentUsage(TimeStampedModel,
                     core_models.DescribableMixin,
                     core_models.UuidMixin):
    resource = models.ForeignKey(on_delete=models.CASCADE, to=Resource, related_name='usages')
    component = models.ForeignKey(on_delete=models.CASCADE, to=OfferingComponent,
                                  limit_choices_to={'billing_type': OfferingComponent.BillingTypes.USAGE})
    usage = models.PositiveIntegerField(default=0)
    date = models.DateTimeField()
    plan_period = models.ForeignKey(on_delete=models.CASCADE, to='ResourcePlanPeriod', related_name='components', null=True)
    billing_period = models.DateField()
    recurring = models.BooleanField(default=False, help_text='Reported value is reused every month until changed.')

    tracker = FieldTracker()

    class Meta:
        unique_together = ('resource', 'component', 'plan_period', 'billing_period')

    def __str__(self):
        return 'resource: %s, component: %s' % (self.resource.name, self.component.name)


class AggregateResourceCount(ScopeMixin):
    """
    This model allows to count current number of project or customer resources by category.
    """
    category = models.ForeignKey(on_delete=models.CASCADE, to=Category, related_name='+')
    count = models.PositiveIntegerField(default=0)
    objects = managers.MixinManager('scope')

    class Meta:
        unique_together = ('category', 'content_type', 'object_id')

    def __str__(self):
        return str(self.category.title)


class OfferingFile(core_models.UuidMixin,
                   core_models.NameMixin,
                   structure_models.StructureModel,
                   TimeStampedModel):
    offering = models.ForeignKey(on_delete=models.CASCADE, to=Offering, related_name='files')
    file = models.FileField(upload_to='offering_files')

    class Permissions:
        customer_path = 'offering__customer'

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering-file'

    def __str__(self):
        return 'offering: %s' % self.offering
