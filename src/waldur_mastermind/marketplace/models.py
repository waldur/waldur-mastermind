from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField, transition
from model_utils import FieldTracker
from model_utils.models import TimeFramedModel, TimeStampedModel
from rest_framework import exceptions as rf_exceptions
from reversion import revisions as reversion

from waldur_core.core import fields as core_fields
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.media.models import get_upload_path
from waldur_core.media.validators import ImageValidator
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure import models as structure_models
from waldur_geo_ip.mixins import CoordinatesMixin
from waldur_pid import mixins as pid_mixins

from ..common import mixins as common_mixins
from . import managers, plugins
from .attribute_types import ATTRIBUTE_TYPES

User = get_user_model()


class ServiceProvider(
    core_models.UuidMixin,
    core_models.DescribableMixin,
    structure_models.ImageModelMixin,
    structure_models.StructureModel,
    TimeStampedModel,
):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)
    api_secret_code = models.CharField(max_length=255, null=True, blank=True)
    lead_email = models.EmailField(
        null=True,
        blank=True,
        help_text=_(
            'Email for notification about new request based order items. '
            'If this field is set, notifications will be sent.'
        ),
    )
    lead_subject = models.CharField(
        max_length=255,
        blank=True,
        help_text=_(
            'Notification subject template. ' 'Django template variables can be used.'
        ),
    )
    lead_body = models.TextField(
        blank=True,
        help_text=_(
            'Notification body template. ' 'Django template variables can be used.'
        ),
        validators=[core_validators.validate_template_syntax],
    )

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
        return (
            Offering.objects.filter(customer=self.customer)
            .exclude(state=Offering.States.ARCHIVED)
            .exists()
        )

    def generate_api_secret_code(self):
        self.api_secret_code = core_utils.pwgen()

    def save(self, *args, **kwargs):
        if not self.pk:
            self.generate_api_secret_code()
        super().save(*args, **kwargs)


class Category(
    core_models.BackendMixin,
    core_models.UuidMixin,
    quotas_models.QuotaModelMixin,
    TimeStampedModel,
):
    title = models.CharField(blank=False, max_length=255)
    icon = models.FileField(
        upload_to='marketplace_category_icons',
        blank=True,
        null=True,
        validators=[ImageValidator],
    )
    description = models.TextField(blank=True)

    default_vm_category = models.BooleanField(
        default=False,
        help_text=_(
            'Set to "true" if this category is for OpenStack VM. Only one category can have "true" value.'
        ),
    )
    default_volume_category = models.BooleanField(
        default=False,
        help_text=_(
            'Set to true if this category is for OpenStack Volume. Only one category can have "true" value.'
        ),
    )
    default_tenant_category = models.BooleanField(
        default=False,
        help_text=_(
            'Set to true if this category is for OpenStack Tenant. Only one category can have "true" value.'
        ),
    )

    def clean_fields(self, exclude=None):
        super().clean_fields(exclude=exclude)

        for flag in [
            'default_volume_category',
            'default_vm_category',
            'default_tenant_category',
        ]:
            if getattr(self, flag):
                category = (
                    Category.objects.filter(**{flag: True}).exclude(id=self.id).first()
                )
                if category:
                    raise ValidationError(
                        {
                            flag: _('%s is already %s.')
                            % (category, flag.replace('_', ' ')),
                        }
                    )

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

    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name='columns'
    )
    index = models.PositiveSmallIntegerField(
        help_text=_('Index allows to reorder columns.')
    )
    title = models.CharField(
        blank=False, max_length=255, help_text=_('Title is rendered as column header.')
    )
    attribute = models.CharField(
        blank=True,
        max_length=255,
        help_text=_('Resource attribute is rendered as table cell.'),
    )
    widget = models.CharField(
        blank=True,
        null=True,
        max_length=255,
        help_text=_('Widget field allows to customise table cell rendering.'),
    )

    def __str__(self):
        return str(self.title)

    def clean(self):
        if not self.attribute and not self.widget:
            raise ValidationError(
                _('Either attribute or widget field should be specified.')
            )


class Section(TimeStampedModel):
    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name='sections'
    )
    is_standalone = models.BooleanField(
        default=False, help_text=_('Whether section is rendered as a separate tab.')
    )

    def __str__(self):
        return str(self.title)


InternalNameValidator = RegexValidator(r'^[a-zA-Z0-9_\-\/:]+$')


class Attribute(TimeStampedModel):
    key = models.CharField(
        primary_key=True, max_length=255, validators=[InternalNameValidator]
    )
    title = models.CharField(blank=False, max_length=255)
    section = models.ForeignKey(
        on_delete=models.CASCADE, to=Section, related_name='attributes'
    )
    type = models.CharField(max_length=255, choices=ATTRIBUTE_TYPES)
    required = models.BooleanField(
        default=False, help_text=_('A value must be provided for the attribute.')
    )
    default = models.JSONField(null=True, blank=True)

    def __str__(self):
        return str(self.title)


class AttributeOption(models.Model):
    attribute = models.ForeignKey(
        Attribute, related_name='options', on_delete=models.CASCADE
    )
    key = models.CharField(max_length=255, validators=[InternalNameValidator])
    title = models.CharField(max_length=255)

    class Meta:
        unique_together = ('attribute', 'key')

    def __str__(self):
        return str(self.title)


class BaseComponent(core_models.DescribableMixin):
    class Meta:
        abstract = True

    name = models.CharField(
        max_length=150,
        help_text=_('Display name for the measured unit, for example, Floating IP.'),
    )
    type = models.CharField(
        max_length=50,
        help_text=_(
            'Unique internal name of the measured unit, for example floating_ip.'
        ),
        validators=[InternalNameValidator],
    )
    measured_unit = models.CharField(
        max_length=30, help_text=_('Unit of measurement, for example, GB.'), blank=True
    )


class CategoryComponent(BaseComponent):
    class Meta:
        unique_together = ('type', 'category')

    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name='components'
    )

    def __str__(self):
        return f'{self.name}, category: {self.category.title}'


class CategoryComponentUsage(core_mixins.ScopeMixin):
    component = models.ForeignKey(on_delete=models.CASCADE, to=CategoryComponent)
    date = models.DateField()
    reported_usage = models.BigIntegerField(null=True)
    fixed_usage = models.BigIntegerField(null=True)
    objects = managers.MixinManager('scope')

    def __str__(self):
        return f'component: {str(self.component.name)}, date: {self.date}'


def offering_has_plans(offering):
    return offering.plans.count() or (offering.parent and offering.parent.plans.count())


class Offering(
    core_models.BackendMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    quotas_models.QuotaModelMixin,
    structure_models.PermissionMixin,
    structure_models.StructureModel,
    TimeStampedModel,
    core_mixins.ScopeMixin,
    LoggableMixin,
    pid_mixins.DataciteMixin,
    CoordinatesMixin,
    structure_models.ImageModelMixin,
):
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

    thumbnail = models.FileField(
        upload_to='marketplace_service_offering_thumbnails',
        blank=True,
        null=True,
        validators=[ImageValidator],
    )
    full_description = models.TextField(blank=True)
    vendor_details = models.TextField(blank=True)
    rating = models.IntegerField(
        null=True,
        validators=[MaxValueValidator(5), MinValueValidator(1)],
        help_text=_('Rating is value from 1 to 5.'),
    )
    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name='offerings'
    )
    customer: structure_models.Customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Customer,
        related_name='+',
        null=True,
    )
    project = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Project,
        related_name='+',
        null=True,
        blank=True,
    )
    # Volume offering is linked with VPC offering via parent field
    parent = models.ForeignKey(
        on_delete=models.CASCADE,
        to='Offering',
        null=True,
        blank=True,
        related_name='children',
    )
    attributes = models.JSONField(
        blank=True, default=dict, help_text=_('Fields describing Category.')
    )
    options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_('Fields describing Offering request form.'),
    )
    plugin_options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            'Public data used by specific plugin, such as storage mode for OpenStack.'
        ),
    )
    secret_options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            'Private data used by specific plugin, such as credentials and hooks.'
        ),
    )

    native_name = models.CharField(max_length=160, default='', blank=True)
    native_description = models.CharField(max_length=500, default='', blank=True)
    terms_of_service = models.TextField(blank=True)
    terms_of_service_link = models.URLField(blank=True)
    privacy_policy_link = models.URLField(blank=True)
    country = models.CharField(max_length=2, blank=True)

    type = models.CharField(max_length=100)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    paused_reason = models.TextField(blank=True)
    divisions = models.ManyToManyField(
        structure_models.Division, related_name='offerings', blank=True
    )

    # If offering is not shared, it is available only to following user categories:
    # 1) staff user;
    # 2) global support user;
    # 3) users with active permission in original customer;
    # 4) users with active permission in related project.
    shared = models.BooleanField(
        default=True, help_text=_('Accessible to all customers.')
    )

    billable = models.BooleanField(
        default=True, help_text=_('Purchase and usage is invoiced.')
    )

    objects = managers.OfferingManager()
    tracker = FieldTracker()

    class Permissions:
        customer_path = 'customer'

    class Meta:
        verbose_name = _('Offering')
        ordering = ['name']

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        order_item_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [OrderItem],
            path_to_scope='offering',
        )

    @transition(
        field=state,
        source=[States.DRAFT, States.PAUSED],
        target=States.ACTIVE,
        conditions=[offering_has_plans],
    )
    def activate(self):
        pass

    @transition(field=state, source=States.ACTIVE, target=States.PAUSED)
    def pause(self):
        pass

    @transition(
        field=state,
        source=States.PAUSED,
        target=States.ACTIVE,
        conditions=[offering_has_plans],
    )
    def unpause(self):
        pass

    @transition(field=state, source='*', target=States.ARCHIVED)
    def archive(self):
        pass

    @transition(field=state, source='*', target=States.DRAFT)
    def draft(self):
        pass

    def __str__(self):
        return str(self.name)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-provider-offering'

    @cached_property
    def component_factors(self):
        # get factor from plugin components
        plugin_components = plugins.manager.get_components(self.type)
        return {c.type: c.factor for c in plugin_components}

    @cached_property
    def is_usage_based(self):
        return self.components.filter(
            billing_type=OfferingComponent.BillingTypes.USAGE,
        ).exists()

    def get_limit_components(self):
        components = self.components.filter(
            billing_type=OfferingComponent.BillingTypes.LIMIT
        )
        return {component.type: component for component in components}

    @cached_property
    def is_limit_based(self):
        if not plugins.manager.can_update_limits(self.type):
            return False
        if not self.components.filter(
            billing_type=OfferingComponent.BillingTypes.LIMIT
        ).exists():
            return False
        return True

    @property
    def is_private(self):
        return not self.billable and not self.shared

    def get_datacite_title(self):
        return self.name

    def get_datacite_creators_name(self):
        return self.customer.name

    def get_datacite_description(self):
        return self.description

    def get_datacite_publication_year(self):
        return self.created.year

    def get_datacite_url(self):
        return core_utils.format_homeport_link(
            'marketplace-public-offering/{offering_uuid}/', offering_uuid=self.uuid.hex
        )

    def can_manage_role(self, user, role=None, timestamp=False):
        if not self.shared:
            return False
        return user.is_staff or self.customer.has_user(
            user, structure_models.CustomerRole.OWNER, timestamp
        )

    def get_users(self, role=None):
        query = Q(offeringpermission__offering=self, offeringpermission__is_active=True)
        return User.objects.filter(query).order_by('username')


class OfferingComponent(
    common_mixins.ProductCodeMixin,
    BaseComponent,
    core_mixins.ScopeMixin,
    core_models.BackendMixin,
):
    class Meta:
        unique_together = ('type', 'offering')
        ordering = ('name',)

    class BillingTypes:
        FIXED = 'fixed'
        USAGE = 'usage'
        ONE_TIME = 'one'
        ON_PLAN_SWITCH = 'few'
        LIMIT = 'limit'

        CHOICES = (
            # if billing type is fixed, service provider specifies exact values of amount field of plan component model
            (FIXED, 'Fixed-price'),
            # if billing type is usage-based billing is applied when usage report is submitted
            (USAGE, 'Usage-based'),
            # if billing type is limit, user specifies limit when resource is provisioned or updated
            (LIMIT, 'Limit-based'),
            # if billing type is one-time, billing is applied once on resource activation
            (ONE_TIME, 'One-time'),
            # applies fee on resource activation and every time a plan has changed, using pricing of a new plan
            (ON_PLAN_SWITCH, 'One-time on plan switch'),
        )

    class LimitPeriods:
        MONTH = 'month'
        ANNUAL = 'annual'
        TOTAL = 'total'

        CHOICES = (
            (
                MONTH,
                'Maximum monthly - every month service provider '
                'can report up to the amount requested by user.',
            ),
            (
                ANNUAL,
                'Maximum annually - every year service provider '
                'can report up to the amount requested by user.',
            ),
            (
                TOTAL,
                'Maximum total - SP can report up to the requested '
                'amount over the whole active state of resource.',
            ),
        )

    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name='components'
    )
    parent = models.ForeignKey(
        on_delete=models.CASCADE, to=CategoryComponent, null=True, blank=True
    )
    billing_type = models.CharField(
        choices=BillingTypes.CHOICES, default=BillingTypes.FIXED, max_length=5
    )
    # limit_period and limit_amount fields are used if billing_type is USAGE or LIMIT
    limit_period = models.CharField(
        choices=LimitPeriods.CHOICES, blank=True, null=True, max_length=6
    )
    limit_amount = models.IntegerField(blank=True, null=True)
    # max_value and min_value fields are used if billing_type is LIMIT
    max_value = models.IntegerField(blank=True, null=True)
    min_value = models.IntegerField(blank=True, null=True)
    max_available_limit = models.IntegerField(blank=True, null=True)
    # is_boolean field allows to render checkbox in UI which set limit amount to 1
    is_boolean = models.BooleanField(default=False)
    # default_limit field is used by UI to prefill limit values
    default_limit = models.IntegerField(blank=True, null=True)
    objects = managers.MixinManager('scope')

    def validate_amount(self, resource, amount, date):
        if not self.limit_period or not self.limit_amount:
            return

        usages = ComponentUsage.objects.filter(resource=resource, component=self)

        if self.limit_period == OfferingComponent.LimitPeriods.MONTH:
            usages = usages.filter(date=core_utils.month_start(date))
        elif self.limit_period == OfferingComponent.LimitPeriods.ANNUAL:
            usages = usages.filter(date__year=date.year)

        total = usages.aggregate(models.Sum('usage'))['usage__sum'] or 0

        if total + amount > self.limit_amount:
            raise rf_exceptions.ValidationError(
                _('Total amount exceeds limit. Total amount: %s, limit: %s.')
                % (total + amount, self.limit_amount)
            )

    @property
    def is_builtin(self):
        return self.type in [
            c.type for c in plugins.manager.get_components(self.offering.type)
        ]

    def __str__(self):
        return str(self.name)


class Plan(
    core_models.BackendMixin,
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.NameMixin,
    core_models.DescribableMixin,
    common_mixins.UnitPriceMixin,
    common_mixins.ProductCodeMixin,
    core_mixins.ScopeMixin,
    LoggableMixin,
):
    """
    Plan unit price is computed as a sum of its fixed components'
    price multiplied by component amount when offering with plans
    is created via REST API. Usage-based components don't contribute to plan price.
    It is assumed that plan price is updated manually when plan component is managed via Django ORM.
    """

    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name='plans'
    )
    archived = models.BooleanField(
        default=False, help_text=_('Forbids creation of new resources.')
    )
    objects = managers.MixinManager('scope')
    max_amount = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1)],
        help_text=_(
            'Maximum number of plans that could be active. '
            'Plan is disabled when maximum amount is reached.'
        ),
    )
    divisions = models.ManyToManyField(
        structure_models.Division, related_name='plans', blank=True
    )
    objects = managers.PlanManager()
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
            components_map = self.offering.get_limit_components()
            component_prices = {
                c.component.type: c.price for c in self.components.all()
            }

            factors = self.offering.component_factors

            for key in components_map.keys():
                price = component_prices.get(key, 0)
                limit = limits.get(key, 0)
                factor = factors.get(key, 1)
                cost += Decimal(price) * limit / factor

        return cost

    def __str__(self):
        return str(self.name)

    @property
    def fixed_price(self):
        return self.sum_components(OfferingComponent.BillingTypes.FIXED)

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
        usage = (
            Resource.objects.filter(plan=self)
            .exclude(state=Resource.States.TERMINATED)
            .count()
        )
        return self.max_amount > usage


class PlanComponent(models.Model):
    class Meta:
        unique_together = ('plan', 'component')
        ordering = ('component__name',)

    plan = models.ForeignKey(
        on_delete=models.CASCADE, to=Plan, related_name='components'
    )
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
        related_name='components',
        null=True,
    )
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(
        default=0,
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal('0'))],
        verbose_name=_('Price per unit per billing period.'),
    )
    tracker = FieldTracker()

    @property
    def has_connected_resources(self):
        return self.plan.has_connected_resources

    def __str__(self):
        name = self.component and self.component.name
        if name:
            return f'{name}, plan: {self.plan.name}'
        return 'for plan: %s' % self.plan.name


class Screenshot(
    core_models.UuidMixin,
    structure_models.StructureModel,
    core_models.DescribableMixin,
    TimeStampedModel,
    core_models.NameMixin,
):
    image = models.ImageField(upload_to=get_upload_path)
    thumbnail = models.ImageField(upload_to=get_upload_path, editable=False, null=True)
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name='screenshots'
    )

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

    # Cost estimate is computed with respect to limit components
    cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan, null=True, blank=True)
    limits = models.JSONField(blank=True, default=dict)

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
        super().init_cost()
        if self.plan:
            if self.type == RequestTypeMixin.Types.CREATE:
                self.cost += self.plan.init_price
            elif self.type == RequestTypeMixin.Types.UPDATE:
                self.cost += self.plan.switch_price

    @property
    def fixed_price(self):
        if self.type == RequestTypeMixin.Types.CREATE:
            return self.plan.fixed_price
        return 0

    @property
    def activation_price(self):
        if self.type == RequestTypeMixin.Types.CREATE:
            return self.plan.init_price
        elif self.type == RequestTypeMixin.Types.UPDATE:
            return self.plan.switch_price
        return 0


class SafeAttributesMixin(models.Model):
    class Meta:
        abstract = True

    offering = models.ForeignKey(Offering, related_name='+', on_delete=models.CASCADE)
    attributes = models.JSONField(blank=True, default=dict)

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


class CartItem(
    SafeAttributesMixin, core_models.UuidMixin, TimeStampedModel, RequestTypeMixin
):
    user = models.ForeignKey(
        core_models.User, related_name='+', on_delete=models.CASCADE
    )
    project = models.ForeignKey(
        structure_models.Project, related_name='+', on_delete=models.CASCADE
    )

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta:
        ordering = ('created',)

    def __str__(self):
        return f'user: {self.user.username}, offering: {self.offering.name}'


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

    created_by = models.ForeignKey(
        on_delete=models.CASCADE, to=core_models.User, related_name='orders'
    )
    approved_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=core_models.User,
        blank=True,
        null=True,
        related_name='+',
    )
    approved_at = models.DateTimeField(editable=False, null=True, blank=True)
    project = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.Project)
    state = FSMIntegerField(
        default=States.REQUESTED_FOR_APPROVAL, choices=States.CHOICES
    )
    total_cost = models.DecimalField(
        max_digits=22, decimal_places=10, null=True, blank=True
    )
    tracker = FieldTracker()

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta:
        verbose_name = _('Order')
        ordering = ('created',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-order'

    @transition(
        field=state, source=States.REQUESTED_FOR_APPROVAL, target=States.EXECUTING
    )
    def approve(self):
        pass

    @transition(field=state, source=States.EXECUTING, target=States.DONE)
    def complete(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATED)
    def terminate(self):
        pass

    @transition(
        field=state, source=States.REQUESTED_FOR_APPROVAL, target=States.REJECTED
    )
    def reject(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def fail(self):
        pass

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
            'uuid',
            'name',
            'created',
            'modified',
            'created_by',
            'approved_by',
            'approved_at',
            'project',
            'get_state_display',
            'total_cost',
        )

    @property
    def type(self):
        if self.items.count():
            return self.items.first().get_type_display()

    def _get_log_context(self, entity_name):
        context = super()._get_log_context(entity_name)
        context['order_items'] = [
            item._get_log_context('') for item in self.items.all()
        ]
        return context

    def __str__(self):
        try:
            return 'project: {}, created by: {}'.format(
                self.project.name,
                self.created_by.username,
            )
        except (KeyError, ObjectDoesNotExist):
            return f'<Order {self.pk}>'


class ResourceDetailsMixin(
    SafeAttributesMixin,
    CostEstimateMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    class Meta:
        abstract = True

    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_(
            'The date is inclusive. Once reached, a resource will be scheduled for termination.'
        ),
    )
    end_date_requested_by = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=core_models.User,
        blank=True,
        null=True,
        related_name='+',
    )


class Resource(
    ResourceDetailsMixin,
    core_models.UuidMixin,
    core_models.BackendMixin,
    TimeStampedModel,
    core_mixins.ScopeMixin,
    structure_models.StructureLoggableMixin,
    common_mixins.BackendMetadataMixin,
):
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

    class Meta:
        ordering = ['created']

    state = FSMIntegerField(default=States.CREATING, choices=States.CHOICES)
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    parent = models.ForeignKey(
        on_delete=models.SET_NULL,
        to='Resource',
        related_name='children',
        null=True,
        blank=True,
    )
    report = models.JSONField(blank=True, null=True)
    current_usages = models.JSONField(blank=True, default=dict)
    tracker = FieldTracker()
    objects = managers.ResourceManager()
    # Effective ID is used when resource is provisioned through remote Waldur
    effective_id = models.CharField(max_length=255, blank=True)
    requested_downscaling = models.BooleanField(default=False)

    @property
    def customer(self):
        return self.project.customer

    @transition(
        field=state,
        source=[States.ERRED, States.CREATING, States.UPDATING, States.TERMINATING],
        target=States.OK,
    )
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
            scope_type = self.scope.get_scope_type()
            return scope_type if scope_type else 'Marketplace.Resource'

    def get_log_fields(self):
        return (
            'uuid',
            'name',
            'project',
            'offering',
            'created',
            'modified',
            'attributes',
            'cost',
            'plan',
            'limits',
            'get_state_display',
            'backend_metadata',
            'backend_uuid',
            'backend_type',
        )

    @property
    def invoice_registrator_key(self):
        return self.offering.type

    @classmethod
    def get_scope_type(cls):
        return 'Marketplace.Resource'

    @classmethod
    def get_url_name(cls):
        return 'marketplace-resource'

    @property
    def is_expired(self):
        return self.end_date and self.end_date <= timezone.datetime.today().date()

    def __str__(self):
        if self.name:
            return f'{self.name} ({self.offering.name})'
        if self.scope:
            return f'{self.name} ({self.content_type} / {self.object_id})'

        return f'{self.uuid} ({self.offering.name})'


class ResourcePlanPeriod(TimeStampedModel, TimeFramedModel, core_models.UuidMixin):
    """
    This model allows to track billing plan for timeframes during resource lifecycle.
    """

    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name='+'
    )
    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan)

    def __str__(self):
        return str(self.resource.name)

    @property
    def current_components(self):
        now = timezone.now()
        return self.components.filter(billing_period=core_utils.month_start(now))


class OrderItem(
    core_models.UuidMixin,
    core_models.BackendMixin,
    core_models.ErrorMessageMixin,
    RequestTypeMixin,
    structure_models.StructureLoggableMixin,
    SafeAttributesMixin,
    TimeStampedModel,
):
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

        TERMINAL_STATES = {DONE, ERRED, TERMINATED}

    order = models.ForeignKey(on_delete=models.CASCADE, to=Order, related_name='items')
    old_plan = models.ForeignKey(
        on_delete=models.CASCADE, to=Plan, related_name='+', null=True, blank=True
    )
    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, null=True, blank=True
    )
    state = FSMIntegerField(default=States.PENDING, choices=States.CHOICES)
    activated = models.DateTimeField(_('activation date'), null=True, blank=True)
    output = models.TextField(blank=True)
    tracker = FieldTracker()

    reviewed_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=core_models.User,
        blank=True,
        null=True,
        related_name='+',
    )
    reviewed_at = models.DateTimeField(editable=False, null=True, blank=True)
    callback_url = models.URLField(null=True, blank=True)
    termination_comment = models.CharField(blank=True, null=True, max_length=255)

    class Permissions:
        customer_path = 'order__project__customer'
        project_path = 'order__project'

    class Meta:
        verbose_name = _('Order item')
        ordering = ('created',)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-order-item'

    @transition(
        field=state, source=[States.PENDING, States.ERRED], target=States.EXECUTING
    )
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
    def set_state_terminated(self, termination_comment=None):
        if termination_comment:
            self.termination_comment = termination_comment
            self.save()
        pass

    @transition(
        field=state,
        source=[States.PENDING, States.EXECUTING],
        target=States.TERMINATING,
    )
    def set_state_terminating(self):
        pass

    def clean(self):
        if self.order.items.count() and self.order.items.first().type != self.type:
            raise ValidationError(_('Types of items in one order must be the same'))

        offering = self.offering
        project = self.order.project
        customer = project.customer

        if offering.shared:
            return

        if offering.customer == customer:
            return

        if offering.project == project:
            return

        raise ValidationError(
            _('Offering "%s" is not allowed in organization "%s".')
            % (offering.name, customer.name)
        )

    def get_log_fields(self):
        return (
            'uuid',
            'created',
            'modified',
            'cost',
            'limits',
            'attributes',
            'offering',
            'resource',
            'plan',
            'get_state_display',
            'get_type_display',
        )

    def __str__(self):
        return 'type: {}, offering: {}, created_by: {}'.format(
            self.get_type_display(),
            self.offering,
            self.order.created_by,
        )


class ComponentQuota(TimeStampedModel):
    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name='quotas'
    )
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
    )
    limit = models.DecimalField(default=-1, decimal_places=2, max_digits=20)
    usage = models.DecimalField(default=0, decimal_places=2, max_digits=20)

    class Meta:
        unique_together = ('resource', 'component')

    def __str__(self):
        return f'resource: {self.resource.name}, component: {self.component.name}'


class ComponentUsage(
    TimeStampedModel,
    core_models.DescribableMixin,
    core_models.BackendMixin,
    core_models.UuidMixin,
    LoggableMixin,
):
    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name='usages'
    )
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
    )
    usage = models.DecimalField(default=0, decimal_places=2, max_digits=20)
    date = models.DateTimeField()
    plan_period = models.ForeignKey(
        on_delete=models.CASCADE,
        to='ResourcePlanPeriod',
        related_name='components',
        null=True,
    )
    billing_period = models.DateField()
    recurring = models.BooleanField(
        default=False, help_text='Reported value is reused every month until changed.'
    )
    modified_by = models.ForeignKey(
        to=User, related_name='+', blank=True, null=True, on_delete=models.SET_NULL
    )

    tracker = FieldTracker()

    class Meta:
        unique_together = ('resource', 'component', 'plan_period', 'billing_period')

    def __str__(self):
        return f'resource: {self.resource.name}, component: {self.component.name}'

    def get_log_fields(self):
        return ('uuid', 'description', 'usage', 'date', 'resource', 'component')


class AggregateResourceCount(core_mixins.ScopeMixin):
    """
    This model allows to count current number of project or customer resources by category.
    """

    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name='+'
    )
    count = models.PositiveIntegerField(default=0)
    objects = managers.MixinManager('scope')

    class Meta:
        unique_together = ('category', 'content_type', 'object_id')

    def __str__(self):
        return str(self.category.title)


class OfferingFile(
    core_models.UuidMixin,
    core_models.NameMixin,
    structure_models.StructureModel,
    TimeStampedModel,
):
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name='files'
    )
    file = models.FileField(upload_to='offering_files')

    class Permissions:
        customer_path = 'offering__customer'

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering-file'

    def __str__(self):
        return 'offering: %s' % self.offering


class OfferingPermission(core_models.UuidMixin, structure_models.BasePermission):
    class Meta:
        unique_together = ('offering', 'user', 'is_active')

    class Permissions:
        customer_path = 'offering__customer'

    offering: Offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name='permissions'
    )
    tracker = FieldTracker(fields=['expiration_time'])

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering-permission'

    def revoke(self):
        self.offering.remove_user(self.user)


class OfferingUser(
    TimeStampedModel,
    common_mixins.BackendMetadataMixin,
):
    offering = models.ForeignKey(Offering, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    username = models.CharField(max_length=100, blank=True, null=True)
    propagation_date = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ('offering', 'user')
        ordering = ['username']

    def get_log_fields(self):
        return ('offering', 'user', 'username')

    def set_propagation_date(self):
        now = timezone.datetime.today()
        self.propagation_date = now

    def __str__(self) -> str:
        return f"{self.offering.name}: {self.username}"


class OfferingUserGroup(TimeStampedModel, common_mixins.BackendMetadataMixin):
    projects = models.ManyToManyField(structure_models.Project, blank=True)
    offering = models.ForeignKey(Offering, on_delete=models.CASCADE)

    def __str__(self):
        return "Offering user group for %s" % self.offering


class CategoryHelpArticle(models.Model):
    title = models.CharField(max_length=255, null=True, blank=True)
    url = models.URLField()
    categories = models.ManyToManyField(Category, related_name='articles', blank=True)

    def __str__(self):
        return self.title


class RobotAccount(
    TimeStampedModel,
    core_models.UuidMixin,
    LoggableMixin,
    common_mixins.BackendMetadataMixin,
    core_models.BackendMixin,
):
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE)
    type = models.CharField(max_length=5)
    # empty string should be allowed because name is set by
    # service provider after the account is created
    username = models.CharField(max_length=32, blank=True)
    users = models.ManyToManyField(User, blank=True)
    keys = models.JSONField(blank=True, default=list)

    tracker = FieldTracker(fields=['resource', 'type', 'username', 'users', 'keys'])

    class Meta:
        unique_together = ('resource', 'type')

    def get_log_fields(self):
        return (
            'type',
            'username',
        )


class OfferingAccessEndpoint(core_models.UuidMixin, core_models.NameMixin):
    url = core_fields.BackendURLField()
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name='endpoints'
    )


class ResourceAccessEndpoint(core_models.UuidMixin, core_models.NameMixin):
    url = core_fields.BackendURLField()
    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name='endpoints'
    )


reversion.register(Screenshot)
reversion.register(OfferingComponent)
reversion.register(PlanComponent)
reversion.register(Plan, follow=('components',))
reversion.register(Offering, follow=('components', 'plans', 'screenshots'))
