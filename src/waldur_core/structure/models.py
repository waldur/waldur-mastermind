import datetime
import itertools
from functools import reduce

import pyvat
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models, transaction
from django.db.models import Q, signals
from django.utils import timezone
from django.utils.lru_cache import lru_cache
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from model_utils.fields import AutoCreatedField
from model_utils.managers import SoftDeletableManagerMixin
from model_utils.models import SoftDeletableModel, TimeStampedModel
from reversion import revisions as reversion

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import COUNTRIES_DICT, JSONField
from waldur_core.core.models import AbstractFieldTracker
from waldur_core.core.shims import TaggableManager
from waldur_core.core.validators import validate_cidr_list, validate_name
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.media.models import ImageModelMixin
from waldur_core.media.validators import CertificateValidator
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure.managers import (
    PrivateServiceSettingsManager,
    ServiceSettingsManager,
    SharedServiceSettingsManager,
    StructureManager,
    filter_queryset_for_user,
)
from waldur_core.structure.registry import SupportedServices, get_resource_type
from waldur_core.structure.signals import structure_role_granted, structure_role_revoked
from waldur_geo_ip.mixins import CoordinatesMixin, IPCoordinatesMixin
from waldur_geo_ip.utils import get_coordinates_by_ip


def validate_service_type(service_type):
    from django.core.exceptions import ValidationError

    if not SupportedServices.has_service_type(service_type):
        raise ValidationError(_('Invalid service type.'))


class StructureModel(models.Model):
    """ Generic structure model.
        Provides transparent interaction with base entities and relations like customer.
    """

    objects = StructureManager()

    class Meta:
        abstract = True

    def __getattr__(self, name):
        # add additional properties to the object according to defined Permissions class
        fields = ('customer', 'project')
        if name in fields:
            try:
                path = getattr(self.Permissions, name + '_path')
            except AttributeError:
                pass
            else:
                if not path == 'self' and '__' in path:
                    return reduce(getattr, path.split('__'), self)

        raise AttributeError(
            "'%s' object has no attribute '%s'" % (self._meta.object_name, name)
        )


class StructureLoggableMixin(LoggableMixin):
    @classmethod
    def get_permitted_objects(cls, user):
        return filter_queryset_for_user(cls.objects.all(), user)


class TagMixin(models.Model):
    """
    Add tags field and manage cache for tags.
    """

    class Meta:
        abstract = True

    tags = TaggableManager(blank=True, related_name='+')

    def get_tags(self):
        key = self._get_tag_cache_key()
        tags = cache.get(key)
        if tags is None:
            tags = list(self.tags.all().values_list('name', flat=True))
            cache.set(key, tags)
        return tags

    def clean_tag_cache(self):
        key = self._get_tag_cache_key()
        cache.delete(key)

    def _get_tag_cache_key(self):
        return 'tags:%s' % core_utils.serialize_instance(self)


class VATException(Exception):
    pass


class VATMixin(models.Model):
    """
    Add country, VAT number fields and check results from EU VAT Information Exchange System.
    Allows to compute VAT charge rate.
    """

    class Meta:
        abstract = True

    vat_code = models.CharField(max_length=20, blank=True, help_text=_('VAT number'))
    vat_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_('Optional business name retrieved for the VAT number.'),
    )
    vat_address = models.CharField(
        max_length=255,
        blank=True,
        help_text=_('Optional business address retrieved for the VAT number.'),
    )

    country = models.CharField(max_length=2, blank=True)

    def get_country_display(self):
        return COUNTRIES_DICT.get(self.country)

    def get_vat_rate(self):
        charge = self.get_vat_charge()
        if charge.action == pyvat.VatChargeAction.charge:
            return charge.rate

        # Return None, if reverse_charge or no_charge action is applied

    def get_vat_charge(self):
        if not self.country:
            raise VATException(
                _(
                    'Unable to get VAT charge because buyer country code is not specified.'
                )
            )

        seller_country = settings.WALDUR_CORE.get('SELLER_COUNTRY_CODE')
        if not seller_country:
            raise VATException(
                _(
                    'Unable to get VAT charge because seller country code is not specified.'
                )
            )

        return pyvat.get_sale_vat_charge(
            datetime.date.today(),
            pyvat.ItemType.generic_electronic_service,
            pyvat.Party(self.country, bool(self.vat_code)),
            pyvat.Party(seller_country, True),
        )


class BasePermission(models.Model):
    class Meta:
        abstract = True

    user = models.ForeignKey(
        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL, db_index=True
    )
    created_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name='+',
    )
    created = AutoCreatedField()
    expiration_time = models.DateTimeField(null=True, blank=True)
    is_active = models.NullBooleanField(default=True, db_index=True)

    @classmethod
    def get_url_name(cls):
        raise NotImplementedError

    @classmethod
    def get_expired(cls):
        return cls.objects.filter(expiration_time__lt=timezone.now(), is_active=True)

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    def revoke(self):
        raise NotImplementedError


class PermissionMixin:
    """
    Base permission management mixin for customer and project.
    It is expected that reverse `permissions` relation is created for this model.
    Provides method to grant, revoke and check object permissions.
    """

    def has_user(self, user, role=None, timestamp=False):
        """
        Checks whether user has role in entity.
        `timestamp` can have following values:
            - False - check whether user has role in entity at the moment.
            - None - check whether user has permanent role in entity.
            - Datetime object - check whether user will have role in entity at specific timestamp.
        """
        permissions = self.get_active_permissions(user, role)

        if timestamp is None:
            permissions = permissions.filter(expiration_time=None)
        elif timestamp:
            permissions = permissions.filter(
                Q(expiration_time=None) | Q(expiration_time__gte=timestamp)
            )

        return permissions.exists()

    @transaction.atomic()
    def add_user(self, user, role=None, created_by=None, expiration_time=None):
        permission = self.get_active_permissions(user, role).first()
        if permission:
            return permission, False

        kwargs = dict(
            user=user,
            is_active=True,
            created_by=created_by,
            expiration_time=expiration_time,
        )
        if role:
            kwargs['role'] = role
        permission = self.permissions.create(**kwargs)

        structure_role_granted.send(
            sender=self.__class__,
            structure=self,
            user=user,
            role=role,
            created_by=created_by,
            expiration_time=expiration_time,
        )

        return permission, True

    @transaction.atomic()
    def remove_user(self, user, role=None, removed_by=None):
        permissions = self.get_active_permissions(user, role)

        affected_permissions = list(permissions)
        permissions.update(is_active=None, expiration_time=timezone.now())

        for permission in affected_permissions:
            self.log_role_revoked(permission, removed_by)

    def get_active_permissions(self, user, role=None):
        permissions = self.permissions.filter(user=user, is_active=True)

        if role is not None:
            permissions = permissions.filter(role=role)
        return permissions

    @transaction.atomic()
    def remove_all_users(self):
        for permission in self.permissions.all().iterator():
            permission.delete()
            self.log_role_revoked(permission)

    def log_role_revoked(self, permission, removed_by=None):
        structure_role_revoked.send(
            sender=self.__class__,
            structure=self,
            user=permission.user,
            role=getattr(permission, 'role', None),
            removed_by=removed_by,
        )

    def can_manage_role(self, user, role=None, timestamp=False):
        """
        Checks whether user can grant/update/revoke permissions for specific role.
        `timestamp` can have following values:
            - False - check whether user can manage permissions at the moment.
            - None - check whether user can permanently manage permissions.
            - Datetime object - check whether user will be able to manage permissions at specific timestamp.
        """
        raise NotImplementedError

    def get_users(self, role=None):
        """ Return all connected to scope users """
        raise NotImplementedError


class CustomerRole(models.CharField):
    OWNER = 'owner'
    SUPPORT = 'support'
    SERVICE_MANAGER = 'service_manager'

    CHOICES = (
        (OWNER, 'Owner'),
        (SUPPORT, 'Support'),
        (SERVICE_MANAGER, 'Service manager'),
    )

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 30
        kwargs['choices'] = self.CHOICES
        super(CustomerRole, self).__init__(*args, **kwargs)


class CustomerPermission(BasePermission):
    class Meta:
        unique_together = ('customer', 'role', 'user', 'is_active')

    class Permissions:
        customer_path = 'customer'

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to='structure.Customer',
        verbose_name=_('organization'),
        related_name='permissions',
    )
    role = CustomerRole(db_index=True)
    tracker = FieldTracker(fields=['expiration_time'])

    @classmethod
    def get_url_name(cls):
        return 'customer_permission'

    def revoke(self):
        self.customer.remove_user(self.user, self.role)

    def __str__(self):
        return '%s | %s' % (self.customer.name, self.get_role_display())


def get_next_agreement_number():
    initial_number = settings.WALDUR_CORE['INITIAL_CUSTOMER_AGREEMENT_NUMBER']
    last_number = Customer.objects.aggregate(models.Max('agreement_number')).get(
        'agreement_number__max'
    )
    return (last_number or initial_number) + 1


class DivisionType(core_models.UuidMixin, core_models.NameMixin, models.Model):
    class Meta:
        verbose_name = _('division type')
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'division-type'

    def __str__(self):
        return self.name


class Division(core_models.UuidMixin, core_models.NameMixin, models.Model):

    type = models.ForeignKey(on_delete=models.CASCADE, to='DivisionType')
    parent = models.ForeignKey(
        on_delete=models.CASCADE, to='Division', null=True, blank=True
    )

    class Meta:
        verbose_name = _('division')
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'division'

    def __str__(self):
        full_path = [self.name]
        d = self.parent

        while d is not None:
            full_path.append(d.name)
            d = d.parent

        return ' -> '.join(full_path[::-1])


CUSTOMER_DETAILS_FIELDS = (
    'name',
    'native_name',
    'abbreviation',
    'contact_details',
    'agreement_number',
    'email',
    'phone_number',
    'access_subnets',
    'registration_code',
    'homepage',
    'domain',
    'vat_code',
    'postal',
    'address',
    'bank_name',
    'bank_account',
    'latitude',
    'longitude',
    'country',
)


class CustomerDetailsMixin(core_models.NameMixin, VATMixin, CoordinatesMixin):
    class Meta:
        abstract = True

    native_name = models.CharField(max_length=160, default='', blank=True)
    abbreviation = models.CharField(max_length=12, blank=True)
    contact_details = models.TextField(blank=True, validators=[MaxLengthValidator(500)])

    agreement_number = models.PositiveIntegerField(null=True, blank=True)
    sponsor_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_('External ID of the sponsor covering the costs'),
    )

    email = models.EmailField(_('email address'), max_length=75, blank=True)
    phone_number = models.CharField(_('phone number'), max_length=255, blank=True)
    access_subnets = models.TextField(
        validators=[validate_cidr_list],
        blank=True,
        default='',
        help_text=_(
            'Enter a comma separated list of IPv4 or IPv6 '
            'CIDR addresses from where connection to self-service is allowed.'
        ),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        help_text=_('Organization identifier in another application.'),
    )
    registration_code = models.CharField(max_length=160, default='', blank=True)
    homepage = models.URLField(max_length=255, blank=True)
    domain = models.CharField(max_length=255, blank=True)

    address = models.CharField(blank=True, max_length=300)
    postal = models.CharField(blank=True, max_length=20)
    bank_name = models.CharField(blank=True, max_length=150)
    bank_account = models.CharField(blank=True, max_length=50)


class Customer(
    CustomerDetailsMixin,
    core_models.UuidMixin,
    core_models.DescendantMixin,
    quotas_models.ExtendableQuotaModelMixin,
    PermissionMixin,
    StructureLoggableMixin,
    ImageModelMixin,
    TimeStampedModel,
    StructureModel,
):
    class Permissions:
        customer_path = 'self'
        project_path = 'projects'

    accounting_start_date = models.DateTimeField(
        _('Start date of accounting'), default=timezone.now
    )
    default_tax_percent = models.DecimalField(
        default=0,
        max_digits=4,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    blocked = models.BooleanField(default=False)
    division = models.ForeignKey(
        'Division', null=True, blank=True, on_delete=models.SET_NULL
    )
    tracker = FieldTracker()

    class Meta:
        verbose_name = _('organization')
        ordering = ('name',)

    GLOBAL_COUNT_QUOTA_NAME = 'nc_global_customer_count'

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        enable_fields_caching = False
        nc_project_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [Project], path_to_scope='customer',
        )
        nc_user_count = quotas_fields.QuotaField()
        nc_resource_count = quotas_fields.CounterQuotaField(
            target_models=lambda: BaseResource.get_all_models(),
            path_to_scope='project.customer',
        )

    def get_log_fields(self):
        return ('uuid', 'name', 'abbreviation', 'contact_details')

    def get_owners(self):
        return self.get_users_by_role(CustomerRole.OWNER)

    def get_support_users(self):
        return self.get_users_by_role(CustomerRole.SUPPORT)

    def get_service_managers(self):
        return self.get_users_by_role(CustomerRole.SERVICE_MANAGER)

    def get_users_by_role(self, role):
        return get_user_model().objects.filter(
            customerpermission__customer=self,
            customerpermission__is_active=True,
            customerpermission__role=role,
        )

    def get_users(self, role=None):
        """ Return all connected to customer users """
        return (
            get_user_model()
            .objects.filter(
                Q(customerpermission__customer=self, customerpermission__is_active=True)
                | Q(
                    projectpermission__project__customer=self,
                    projectpermission__is_active=True,
                )
            )
            .distinct()
            .order_by('username')
        )

    def can_user_update_quotas(self, user):
        return user.is_staff

    def can_manage_role(self, user, role=None, timestamp=False):
        return user.is_staff or (
            self.has_user(user, CustomerRole.OWNER, timestamp)
            and settings.WALDUR_CORE['OWNERS_CAN_MANAGE_OWNERS']
        )

    def get_children(self):
        return itertools.chain.from_iterable(
            m.objects.filter(customer=self) for m in [Project]
        )

    def is_billable(self):
        return timezone.now() >= self.accounting_start_date

    @classmethod
    def get_permitted_objects(cls, user):
        if user.is_staff or user.is_support:
            return cls.objects.all()
        else:
            return cls.objects.filter(
                permissions__user=user,
                permissions__role=CustomerRole.OWNER,
                permissions__is_active=True,
            )

    def get_display_name(self):
        if self.abbreviation:
            return self.abbreviation
        if self.domain:
            return '{name} ({domain})'.format(name=self.name, domain=self.domain)
        return self.name

    def delete(self, *args, **kwargs):
        """Delete customers' projects if they all mark as 'removed'."""
        if not self.projects.count():
            for project in Project.structure_objects.filter(customer=self):
                project.delete(soft=False)

        return super(Customer, self).delete(*args, **kwargs)

    def __str__(self):
        if self.abbreviation:
            return '%(name)s (%(abbreviation)s)' % {
                'name': self.name,
                'abbreviation': self.abbreviation,
            }
        else:
            return self.name


class ProjectRole(models.CharField):
    ADMINISTRATOR = 'admin'
    MANAGER = 'manager'
    MEMBER = 'member'

    CHOICES = (
        (ADMINISTRATOR, 'Administrator'),
        (MANAGER, 'Manager'),
        (MEMBER, 'Member'),
    )

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 30
        kwargs['choices'] = self.CHOICES
        super(ProjectRole, self).__init__(*args, **kwargs)


class ProjectPermission(core_models.UuidMixin, BasePermission):
    class Meta:
        unique_together = ('project', 'role', 'user', 'is_active')

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    project = models.ForeignKey(
        on_delete=models.CASCADE, to='structure.Project', related_name='permissions'
    )
    role = ProjectRole(db_index=True)
    tracker = FieldTracker(fields=['expiration_time'])

    @classmethod
    def get_url_name(cls):
        return 'project_permission'

    def revoke(self):
        self.project.remove_user(self.user, self.role)

    def __str__(self):
        project = Project.all_objects.get(
            id=self.project_id
        )  # handle case when project is already deleted
        return '%s | %s' % (project.name, self.get_role_display())


class ProjectType(
    core_models.DescribableMixin, core_models.UuidMixin, core_models.NameMixin
):
    class Meta:
        verbose_name = _('Project type')
        verbose_name_plural = _('Project types')
        ordering = ['name']

    @classmethod
    def get_url_name(cls):
        return 'project_type'

    def __str__(self):
        return self.name


class SoftDeletableManager(SoftDeletableManagerMixin, StructureManager):
    pass


PROJECT_NAME_LENGTH = 500

PROJECT_DETAILS_FIELDS = (
    'name',
    'description',
    'end_date',
    'type',
    'oecd_fos_2007_code',
)


class ProjectDetailsMixin(core_models.DescribableMixin):
    class Meta:
        abstract = True

    OECD_FOS_2007_CODES = (
        ('1.1', _('Mathematics')),
        ('1.2', _('Computer and information sciences')),
        ('1.3', _('Physical sciences')),
        ('1.4', _('Chemical sciences')),
        ('1.5', _('Earth and related environmental sciences')),
        ('1.6', _('Biological sciences')),
        ('1.7', _('Other natural sciences')),
        ('2.1', _('Civil engineering')),
        (
            '2.2',
            _(
                'Electrical engineering, electronic engineering, information engineering'
            ),
        ),
        ('2.3', _('Mechanical engineering')),
        ('2.4', _('Chemical engineering')),
        ('2.5', _('Materials engineering')),
        ('2.6', _('Medical engineering')),
        ('2.7', _('Environmental engineering')),
        ('2.8', _('Systems engineering')),
        ('2.9', _('Environmental biotechnology')),
        ('2.10', _('Industrial biotechnology')),
        ('2.11', _('Nano technology')),
        ('2.12', _('Other engineering and technologies')),
        ('3.1', _('Basic medicine')),
        ('3.2', _('Clinical medicine')),
        ('3.3', _('Health sciences')),
        ('3.4', _('Health biotechnology')),
        ('3.5', _('Other medical sciences')),
        ('4.1', _('Agriculture, forestry, and fisheries')),
        ('4.2', _('Animal and dairy science')),
        ('4.3', _('Veterinary science')),
        ('4.4', _('Agricultural biotechnology')),
        ('4.5', _('Other agricultural sciences')),
        ('5.1', _('Psychology')),
        ('5.2', _('Economics and business')),
        ('5.3', _('Educational sciences')),
        ('5.4', _('Sociology')),
        ('5.5', _('Law')),
        ('5.6', _('Political science')),
        ('5.7', _('Social and economic geography')),
        ('5.8', _('Media and communications')),
        ('5.9', _('Other social sciences')),
        ('6.1', _('History and archaeology')),
        ('6.2', _('Languages and literature')),
        ('6.3', _('Philosophy, ethics and religion')),
        ('6.4', _('Arts (arts, history of arts, performing arts, music)')),
        ('6.5', _('Other humanities')),
    )

    # NameMixin is not used because it has too strict limitation for max_length.
    name = models.CharField(
        _('name'), max_length=PROJECT_NAME_LENGTH, validators=[validate_name]
    )

    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_(
            'The date is inclusive. Once reached, all project resource will be scheduled for termination.'
        ),
    )
    type = models.ForeignKey(
        ProjectType,
        verbose_name=_('project type'),
        blank=True,
        null=True,
        on_delete=models.PROTECT,
    )
    oecd_fos_2007_code = models.CharField(
        choices=OECD_FOS_2007_CODES, null=True, blank=True, max_length=80
    )


class Project(
    ProjectDetailsMixin,
    core_models.UuidMixin,
    core_models.DescendantMixin,
    core_models.BackendMixin,
    quotas_models.ExtendableQuotaModelMixin,
    PermissionMixin,
    StructureLoggableMixin,
    TimeStampedModel,
    StructureModel,
    SoftDeletableModel,
):
    class Permissions:
        customer_path = 'customer'
        project_path = 'self'

    GLOBAL_COUNT_QUOTA_NAME = 'nc_global_project_count'

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        enable_fields_caching = False
        nc_resource_count = quotas_fields.CounterQuotaField(
            target_models=lambda: BaseResource.get_all_models(),
            path_to_scope='project',
        )

    customer = models.ForeignKey(
        Customer,
        verbose_name=_('organization'),
        related_name='projects',
        on_delete=models.PROTECT,
    )
    tracker = FieldTracker()
    objects = SoftDeletableManager()
    structure_objects = StructureManager()

    @property
    def is_expired(self):
        return self.end_date and self.end_date <= timezone.datetime.today().date()

    @property
    def full_name(self):
        return self.name

    def get_users(self, role=None):
        query = Q(projectpermission__project=self, projectpermission__is_active=True,)
        if role:
            query = query & Q(projectpermission__role=role)

        return get_user_model().objects.filter(query).order_by('username')

    @transaction.atomic()
    def _soft_delete(self, using=None):
        """ Method for project soft delete. It doesn't delete a project, only mark as 'removed', but it sends signals
        """
        signals.pre_delete.send(sender=self.__class__, instance=self, using=using)

        self.is_removed = True
        self.save(using=using)

        signals.post_delete.send(sender=self.__class__, instance=self, using=using)

    def delete(self, using=None, soft=True, *args, **kwargs):
        """Use soft delete, i.e. mark a project as 'removed'."""
        if soft:
            self._soft_delete(using)
        else:
            return super(SoftDeletableModel, self).delete(using=using, *args, **kwargs)

    def __str__(self):
        return '%(name)s | %(customer)s' % {
            'name': self.name,
            'customer': self.customer.name,
        }

    def can_user_update_quotas(self, user):
        return user.is_staff or self.customer.has_user(user, CustomerRole.OWNER)

    def can_manage_role(self, user, role=None, timestamp=False):
        if user.is_staff:
            return True
        if self.customer.has_user(user, CustomerRole.OWNER, timestamp):
            return True

        return role == ProjectRole.ADMINISTRATOR and self.has_user(
            user, ProjectRole.MANAGER, timestamp
        )

    def get_log_fields(self):
        return ('uuid', 'customer', 'name')

    def get_parents(self):
        return [self.customer]

    class Meta:
        base_manager_name = 'objects'


class CustomerPermissionReview(core_models.UuidMixin):
    class Permissions:
        customer_path = 'customer'

    customer = models.ForeignKey(
        Customer,
        verbose_name=_('organization'),
        related_name='reviews',
        on_delete=models.CASCADE,
    )
    is_pending = models.BooleanField(default=True)
    created = AutoCreatedField()
    closed = models.DateTimeField(null=True, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    @classmethod
    def get_url_name(cls):
        return 'customer_permission_review'

    def close(self, user):
        self.is_pending = False
        self.closed = timezone.now()
        self.reviewer = user
        self.save()


def build_service_settings_query(user):
    return (
        Q(shared=True)
        | Q(shared=False, customer__projects__permissions__user=user, is_active=True,)
        | Q(shared=False, customer__permissions__user=user, is_active=True)
    )


class ServiceSettings(
    quotas_models.ExtendableQuotaModelMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.StateMixin,
    TagMixin,
    StructureLoggableMixin,
):
    class Meta:
        verbose_name = "Service settings"
        verbose_name_plural = "Service settings"
        ordering = ('name',)

    class Permissions:
        customer_path = 'customer'
        build_query = build_service_settings_query

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Customer,
        verbose_name=_('organization'),
        related_name='service_settings',
        blank=True,
        null=True,
    )
    backend_url = core_fields.BackendURLField(max_length=200, blank=True, null=True)
    username = models.CharField(max_length=100, blank=True, null=True)
    password = models.CharField(max_length=100, blank=True, null=True)
    domain = models.CharField(max_length=200, blank=True, null=True)
    token = models.CharField(max_length=255, blank=True, null=True)
    certificate = models.FileField(
        upload_to='certs', blank=True, null=True, validators=[CertificateValidator]
    )
    type = models.CharField(
        max_length=255, db_index=True, validators=[validate_service_type]
    )
    options = JSONField(default=dict, help_text=_('Extra options'), blank=True)
    shared = models.BooleanField(default=False, help_text=_('Anybody can use it'))
    terms_of_services = models.URLField(max_length=255, blank=True)

    tracker = FieldTracker()

    # service settings scope - VM that contains service
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')

    objects = ServiceSettingsManager('scope')

    is_active = models.BooleanField(
        default=True,
        help_text='Information about inactive service settings will not be updated in the background',
    )

    def get_backend(self, **kwargs):
        return SupportedServices.get_service_backend(self.type)(self, **kwargs)

    def get_option(self, name):
        options = self.options or {}
        if name in options:
            return options.get(name)
        else:
            defaults = self.get_backend().DEFAULTS
            return defaults.get(name)

    def __str__(self):
        return '%s (%s)' % (self.name, self.type)

    def get_log_fields(self):
        return ('uuid', 'name', 'customer')

    def _get_log_context(self, entity_name):
        context = super(ServiceSettings, self)._get_log_context(entity_name)
        context['service_settings_type'] = self.type
        return context

    def get_type_display(self):
        return self.type


class SharedServiceSettings(ServiceSettings):
    """Required for a clear separation of shared/private service settings on admin."""

    objects = SharedServiceSettingsManager()

    class Meta:
        proxy = True
        verbose_name_plural = _('Shared provider settings')


class PrivateServiceSettings(ServiceSettings):
    """Required for a clear separation of shared/private service settings on admin."""

    objects = PrivateServiceSettingsManager()

    class Meta:
        proxy = True
        verbose_name_plural = _('Private provider settings')


class BaseServiceProperty(
    core_models.BackendModelMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    models.Model,
):
    """ Base service properties like image, flavor, region,
        which are usually used for Resource provisioning.
    """

    class Meta:
        abstract = True

    @classmethod
    def get_url_name(cls):
        """ This name will be used by generic relationships to membership model for URL creation """
        return '{}-{}'.format(cls._meta.app_label, cls.__name__.lower())

    @classmethod
    def get_backend_fields(cls):
        return super(BaseServiceProperty, cls).get_backend_fields() + (
            'backend_id',
            'name',
        )


class ServiceProperty(BaseServiceProperty):
    class Meta:
        abstract = True
        unique_together = ('settings', 'backend_id')

    settings = models.ForeignKey(
        on_delete=models.CASCADE, to=ServiceSettings, related_name='+'
    )
    backend_id = models.CharField(max_length=255, db_index=True)

    def __str__(self):
        return '{0} | {1}'.format(self.name, self.settings)


class GeneralServiceProperty(BaseServiceProperty):
    """
    Service property which is not connected to settings
    """

    class Meta:
        abstract = True

    backend_id = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name


class BaseResource(
    core_models.UuidMixin,
    core_models.DescribableMixin,
    core_models.NameMixin,
    core_models.DescendantMixin,
    core_models.BackendModelMixin,
    core_models.StateMixin,
    StructureLoggableMixin,
    TagMixin,
    TimeStampedModel,
    StructureModel,
):

    """ Base resource class. Resource is a provisioned entity of a service,
        for example: a VM in OpenStack or AWS, or a repository in Github.
    """

    class Meta:
        abstract = True
        ordering = ['-created']

    class Permissions:
        customer_path = 'project__customer'
        project_path = 'project'

    service_settings = models.ForeignKey(
        on_delete=models.CASCADE, to=ServiceSettings, related_name='+'
    )
    project = models.ForeignKey(on_delete=models.CASCADE, to=Project, related_name='+')
    backend_id = models.CharField(max_length=255, blank=True)

    @classmethod
    def get_backend_fields(cls):
        return super(BaseResource, cls).get_backend_fields() + ('backend_id',)

    def get_backend(self, **kwargs):
        return self.service_settings.get_backend(**kwargs)

    def get_access_url(self):
        # default behaviour. Override in subclasses if applicable
        return None

    def get_access_url_name(self):
        return None

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @classmethod
    def get_url_name(cls):
        """ This name will be used by generic relationships to membership model for URL creation """
        return '{}-{}'.format(cls._meta.app_label, cls.__name__.lower())

    def get_log_fields(self):
        return ('uuid', 'name', 'service_settings', 'project', 'full_name')

    @property
    def full_name(self):
        return '%s %s' % (get_resource_type(self).replace('.', ' '), self.name,)

    def _get_log_context(self, entity_name):
        context = super(BaseResource, self)._get_log_context(entity_name)
        # XXX: Add resource_full_name here, because event context does not support properties as fields
        context['resource_full_name'] = self.full_name
        context['resource_type'] = get_resource_type(self)

        return context

    def get_parents(self):
        project = Project.all_objects.get(id=self.project_id)
        return [self.service_settings, project]

    def __str__(self):
        return self.name

    def increase_backend_quotas_usage(self, validate=True):
        """ Increase usage of quotas that were consumed by resource on creation.

            If validate is True - method should raise QuotaValidationError if
            at least one of increased quotas if over limit.
        """
        pass

    def decrease_backend_quotas_usage(self):
        """ Decrease usage of quotas that were released on resource deletion """
        pass

    @classmethod
    def get_scope_type(cls):
        return get_resource_type(cls)


class VirtualMachine(IPCoordinatesMixin, core_models.RuntimeStateMixin, BaseResource):
    def __init__(self, *args, **kwargs):
        AbstractFieldTracker().finalize_class(self.__class__, 'tracker')
        super(VirtualMachine, self).__init__(*args, **kwargs)

    cores = models.PositiveSmallIntegerField(
        default=0, help_text=_('Number of cores in a VM')
    )
    ram = models.PositiveIntegerField(default=0, help_text=_('Memory size in MiB'))
    disk = models.PositiveIntegerField(default=0, help_text=_('Disk size in MiB'))
    min_ram = models.PositiveIntegerField(
        default=0, help_text=_('Minimum memory size in MiB')
    )
    min_disk = models.PositiveIntegerField(
        default=0, help_text=_('Minimum disk size in MiB')
    )

    image_name = models.CharField(max_length=150, blank=True)

    key_name = models.CharField(max_length=50, blank=True)
    key_fingerprint = models.CharField(max_length=47, blank=True)

    user_data = models.TextField(
        blank=True,
        help_text=_('Additional data that will be added to instance on provisioning'),
    )
    start_time = models.DateTimeField(blank=True, null=True)

    class Meta:
        abstract = True

    def detect_coordinates(self):
        if self.external_ips:
            return get_coordinates_by_ip(self.external_ips)

    def get_access_url(self):
        if self.external_ips:
            return self.external_ips
        if self.internal_ips:
            return self.internal_ips
        return None

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @property
    def external_ips(self):
        """
        Returns a list of external IPs.
        Implementation of this method in all derived classes guarantees all virtual machine have the same interface.
        For instance:
         - SaltStack (aws) handles IPs as private and public IP addresses;
         - DigitalOcean has only 1 external ip called ip_address.
        """
        return []

    @property
    def internal_ips(self):
        """
        Returns a list of internal IPs.
        Implementation of this method in all derived classes guarantees all virtual machine have the same interface.
        For instance:
         - SaltStack (aws) handles IPs as private and public IP addresses;
         - DigitalOcean does not support internal IP at the moment.
        """
        return []


class PrivateCloud(
    quotas_models.QuotaModelMixin, core_models.RuntimeStateMixin, BaseResource
):
    class Meta:
        abstract = True


class Storage(core_models.RuntimeStateMixin, BaseResource):
    size = models.PositiveIntegerField(help_text=_('Size in MiB'))

    class Meta:
        abstract = True


class Volume(Storage):
    class Meta:
        abstract = True


class Snapshot(Storage):
    class Meta:
        abstract = True


class SubResource(BaseResource):
    """ Resource dependent object that cannot exist without resource. """

    class Meta:
        abstract = True

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]


reversion.register(Customer)
