import datetime
from functools import lru_cache

import pyvat
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models, transaction
from django.db.models import Q, signals
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.fields import AutoCreatedField
from model_utils.managers import SoftDeletableManagerMixin
from model_utils.models import SoftDeletableModel, TimeStampedModel
from netfields import CidrAddressField, NetManager
from reversion import revisions as reversion

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core.fields import COUNTRIES_DICT, JSONField
from waldur_core.core.models import AbstractFieldTracker
from waldur_core.core.validators import validate_cidr_list, validate_name
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.media.models import ImageModelMixin
from waldur_core.media.validators import CertificateValidator
from waldur_core.permissions.enums import SYSTEM_PROJECT_ROLES, RoleEnum
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import (
    add_user,
    delete_user,
    get_permissions,
    has_user,
)
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure.managers import (
    PrivateServiceSettingsManager,
    ServiceSettingsManager,
    SharedServiceSettingsManager,
    StructureManager,
    filter_queryset_for_user,
    get_connected_customers,
    get_customer_users,
    get_nested_customer_users,
    get_project_users,
    get_visible_customers,
)
from waldur_core.structure.registry import SupportedServices, get_resource_type
from waldur_geo_ip.mixins import CoordinatesMixin, IPCoordinatesMixin
from waldur_geo_ip.utils import get_coordinates_by_ip


def validate_service_type(service_type):
    from django.core.exceptions import ValidationError

    if not SupportedServices.has_service_type(service_type):
        raise ValidationError(_("Invalid service type."))


class StructureLoggableMixin(LoggableMixin):
    @classmethod
    def get_permitted_objects(cls, user):
        return filter_queryset_for_user(cls.objects.all(), user)


class VATException(Exception):
    pass


class VATMixin(models.Model):
    """
    Add country, VAT number fields and check results from EU VAT Information Exchange System.
    Allows to compute VAT charge rate.
    """

    class Meta:
        abstract = True

    vat_code = models.CharField(max_length=20, blank=True, help_text=_("VAT number"))
    vat_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Optional business name retrieved for the VAT number."),
    )
    vat_address = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Optional business address retrieved for the VAT number."),
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
                    "Unable to get VAT charge because buyer country code is not specified."
                )
            )

        seller_country = settings.WALDUR_CORE.get("SELLER_COUNTRY_CODE")
        if not seller_country:
            raise VATException(
                _(
                    "Unable to get VAT charge because seller country code is not specified."
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
        related_name="+",
    )
    created = AutoCreatedField()
    expiration_time = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(null=True, default=True, db_index=True)

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

    def get_or_create_role(self, role=None):
        if role and isinstance(role, str):
            return Role.objects.get_system_role(
                name=get_new_role_name(self._meta.model, role),
                content_type=ContentType.objects.get_for_model(self._meta.model),
            )
        return role

    def has_user(self, user, role=None, timestamp=False):
        role = self.get_or_create_role(role)
        return has_user(self, user, role, timestamp)

    @transaction.atomic()
    def add_user(self, user, role, created_by=None, expiration_time=None):
        role = self.get_or_create_role(role)
        permission = add_user(self, user, role, created_by, expiration_time)
        return permission

    @transaction.atomic()
    def remove_user(self, user, role=None, removed_by=None):
        role = self.get_or_create_role(role)
        if role:
            delete_user(self, user, role, removed_by)
        else:
            for perm in get_permissions(self, user):
                perm.revoke(removed_by)

    def get_users(self, role=None):
        """Return all connected to scope users"""
        raise NotImplementedError

    def get_user_mails(self, role=None):
        return (
            self.get_users(role)
            .exclude(email="")
            .exclude(notifications_enabled=False)
            .values_list("email", flat=True)
        )


class CustomerRole(models.CharField):
    OWNER = "owner"
    SUPPORT = "support"
    SERVICE_MANAGER = "service_manager"

    CHOICES = (
        (OWNER, "Owner"),
        (SUPPORT, "Support"),
        (SERVICE_MANAGER, "Service manager"),
    )

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 30
        kwargs["choices"] = self.CHOICES
        super().__init__(*args, **kwargs)


class OrganizationGroupType(core_models.UuidMixin, core_models.NameMixin, models.Model):
    class Meta:
        verbose_name = _("organization group type")
        ordering = ("name",)

    @classmethod
    def get_url_name(cls):
        return "organization-group-type"

    def __str__(self):
        return self.name


class OrganizationGroup(core_models.UuidMixin, core_models.NameMixin, models.Model):
    type = models.ForeignKey(on_delete=models.CASCADE, to="OrganizationGroupType")
    parent = models.ForeignKey(
        on_delete=models.CASCADE, to="OrganizationGroup", null=True, blank=True
    )

    class Meta:
        verbose_name = _("organization group")
        ordering = ("name",)

    @classmethod
    def get_url_name(cls):
        return "organization-group"

    def __str__(self):
        full_path = [self.name]
        d = self.parent

        while d is not None:
            full_path.append(d.name)
            d = d.parent

        return " -> ".join(full_path[::-1])


CUSTOMER_DETAILS_FIELDS = (
    "name",
    "native_name",
    "abbreviation",
    "contact_details",
    "agreement_number",
    "email",
    "phone_number",
    "access_subnets",
    "registration_code",
    "homepage",
    "domain",
    "vat_code",
    "postal",
    "address",
    "bank_name",
    "bank_account",
    "latitude",
    "longitude",
    "country",
)


class AccessSubnet(core_models.UuidMixin, core_models.DescribableMixin, LoggableMixin):
    customer = models.ForeignKey(
        on_delete=models.CASCADE, to="Customer", related_name="access_subnet_set"
    )
    inet = CidrAddressField(null=True, blank=True)
    tracker = FieldTracker()

    class Meta:
        unique_together = ("customer", "inet")

    def __str__(self):
        return self.customer.name + " | " + str(self.inet)

    def get_log_fields(self):
        return "description", "inet"


class CustomerDetailsMixin(core_models.NameMixin, VATMixin, CoordinatesMixin):
    class Meta:
        abstract = True

    native_name = models.CharField(max_length=160, default="", blank=True)
    abbreviation = models.CharField(max_length=12, blank=True)
    contact_details = models.TextField(blank=True, validators=[MaxLengthValidator(500)])

    agreement_number = models.CharField(max_length=160, default="", blank=True)
    sponsor_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("External ID of the sponsor covering the costs"),
    )

    email = models.EmailField(_("email address"), max_length=75, blank=True)
    phone_number = models.CharField(_("phone number"), max_length=255, blank=True)
    access_subnets = models.TextField(
        validators=[validate_cidr_list],
        blank=True,
        default="",
        help_text=_(
            "Enter a comma separated list of IPv4 or IPv6 "
            "CIDR addresses from where connection to self-service is allowed."
        ),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Organization identifier in another application."),
    )
    registration_code = models.CharField(max_length=160, default="", blank=True)
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
):
    class Permissions:
        customer_path = "self"
        project_path = "projects"

    accounting_start_date = models.DateTimeField(
        _("Start date of accounting"), default=timezone.now
    )
    default_tax_percent = models.DecimalField(
        default=0,
        max_digits=4,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(200)],
    )
    blocked = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    organization_group = models.ForeignKey(
        "OrganizationGroup", null=True, blank=True, on_delete=models.SET_NULL
    )
    tracker = FieldTracker()
    objects = NetManager()

    class Meta:
        verbose_name = _("organization")
        ordering = ("name",)

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        enable_fields_caching = False
        nc_project_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [Project],
            path_to_scope="customer",
        )
        nc_user_count = quotas_fields.QuotaField()
        nc_resource_count = quotas_fields.CounterQuotaField(
            target_models=lambda: BaseResource.get_all_models(),
            path_to_scope="project.customer",
        )

    def get_log_fields(self):
        return ("uuid", "name", "abbreviation", "contact_details")

    def get_owner_mails(self):
        return (
            self.get_users(RoleEnum.CUSTOMER_OWNER)
            .exclude(email="")
            .exclude(notifications_enabled=False)
            .values_list("email", flat=True)
        )

    def get_users(self, role=None):
        """Return all connected to customer users"""
        if role:
            users = get_customer_users(self.id, role)
            return get_user_model().objects.filter(id__in=users)

        return (
            get_user_model()
            .objects.filter(id__in=get_nested_customer_users(self))
            .distinct()
            .order_by("username")
        )

    def is_billable(self):
        return timezone.now() >= self.accounting_start_date

    @classmethod
    def get_permitted_objects(cls, user):
        if user.is_staff or user.is_support:
            return cls.objects.all()
        else:
            return Customer.objects.filter(
                id__in=get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)
            )

    def get_display_name(self):
        if self.abbreviation:
            return self.abbreviation
        if self.domain:
            return f"{self.name} ({self.domain})"
        return self.name

    def delete(self, *args, **kwargs):
        """Delete customers' projects if they all mark as 'removed'."""
        if Project.available_objects.filter(customer=self).count() == 0:
            for project in Project.objects.filter(customer=self):
                project.delete(soft=False)

        return super().delete(*args, **kwargs)

    def __str__(self):
        if self.abbreviation:
            return f"{self.name} ({self.abbreviation})"
        else:
            return self.name


class ProjectRole(models.CharField):
    ADMINISTRATOR = "admin"
    MANAGER = "manager"
    MEMBER = "member"

    CHOICES = (
        (ADMINISTRATOR, "Administrator"),
        (MANAGER, "Manager"),
        (MEMBER, "Member"),
    )

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 30
        kwargs["choices"] = self.CHOICES
        super().__init__(*args, **kwargs)


class ProjectType(
    core_models.DescribableMixin, core_models.UuidMixin, core_models.NameMixin
):
    class Meta:
        verbose_name = _("Project type")
        verbose_name_plural = _("Project types")
        ordering = ["name"]

    @classmethod
    def get_url_name(cls):
        return "project_type"

    def __str__(self):
        return self.name


class SoftDeletableManager(SoftDeletableManagerMixin, StructureManager):
    pass


PROJECT_NAME_LENGTH = 500

PROJECT_DETAILS_FIELDS = (
    "name",
    "description",
    "end_date",
    "oecd_fos_2007_code",
    "is_industry",
)


class ProjectOECDFOS2007CodeMixin(models.Model):
    class Meta:
        abstract = True

    OECD_FOS_2007_CODES = (
        ("1.1", _("Mathematics")),
        ("1.2", _("Computer and information sciences")),
        ("1.3", _("Physical sciences")),
        ("1.4", _("Chemical sciences")),
        ("1.5", _("Earth and related environmental sciences")),
        ("1.6", _("Biological sciences")),
        ("1.7", _("Other natural sciences")),
        ("2.1", _("Civil engineering")),
        (
            "2.2",
            _(
                "Electrical engineering, electronic engineering, information engineering"
            ),
        ),
        ("2.3", _("Mechanical engineering")),
        ("2.4", _("Chemical engineering")),
        ("2.5", _("Materials engineering")),
        ("2.6", _("Medical engineering")),
        ("2.7", _("Environmental engineering")),
        ("2.8", _("Systems engineering")),
        ("2.9", _("Environmental biotechnology")),
        ("2.10", _("Industrial biotechnology")),
        ("2.11", _("Nano technology")),
        ("2.12", _("Other engineering and technologies")),
        ("3.1", _("Basic medicine")),
        ("3.2", _("Clinical medicine")),
        ("3.3", _("Health sciences")),
        ("3.4", _("Health biotechnology")),
        ("3.5", _("Other medical sciences")),
        ("4.1", _("Agriculture, forestry, and fisheries")),
        ("4.2", _("Animal and dairy science")),
        ("4.3", _("Veterinary science")),
        ("4.4", _("Agricultural biotechnology")),
        ("4.5", _("Other agricultural sciences")),
        ("5.1", _("Psychology")),
        ("5.2", _("Economics and business")),
        ("5.3", _("Educational sciences")),
        ("5.4", _("Sociology")),
        ("5.5", _("Law")),
        ("5.6", _("Political science")),
        ("5.7", _("Social and economic geography")),
        ("5.8", _("Media and communications")),
        ("5.9", _("Other social sciences")),
        ("6.1", _("History and archaeology")),
        ("6.2", _("Languages and literature")),
        ("6.3", _("Philosophy, ethics and religion")),
        ("6.4", _("Arts (arts, history of arts, performing arts, music)")),
        ("6.5", _("Other humanities")),
    )

    OECD_FOS_2007_CODES_DICT = dict(OECD_FOS_2007_CODES)
    oecd_fos_2007_code = models.CharField(
        choices=OECD_FOS_2007_CODES, null=True, blank=True, max_length=80
    )


class ProjectDetailsMixin(core_models.DescribableMixin, ProjectOECDFOS2007CodeMixin):
    class Meta:
        abstract = True

    # NameMixin is not used because it has too strict limitation for max_length.
    name = models.CharField(
        _("name"), max_length=PROJECT_NAME_LENGTH, validators=[validate_name]
    )

    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_(
            "The date is inclusive. Once reached, all project resource will be scheduled for termination."
        ),
    )
    end_date_requested_by = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=core_models.User,
        blank=True,
        null=True,
        related_name="+",
    )
    type = models.ForeignKey(
        ProjectType,
        verbose_name=_("project type"),
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )
    is_industry = models.BooleanField(default=False)


class Project(
    ProjectDetailsMixin,
    core_models.UuidMixin,
    core_models.DescendantMixin,
    core_models.BackendMixin,
    quotas_models.ExtendableQuotaModelMixin,
    PermissionMixin,
    StructureLoggableMixin,
    ImageModelMixin,
    TimeStampedModel,
    SoftDeletableModel,
):
    class Permissions:
        customer_path = "customer"
        project_path = "self"

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        enable_fields_caching = False
        nc_resource_count = quotas_fields.CounterQuotaField(
            target_models=lambda: BaseResource.get_all_models(),
            path_to_scope="project",
        )

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("organization"),
        related_name="projects",
        on_delete=models.CASCADE,
    )
    tracker = FieldTracker()
    # Entities returned in manager available_objects are limited to not-deleted instances.
    # Entities returned in manager objects include deleted objects.
    available_objects = SoftDeletableManager()
    objects = StructureManager()

    @property
    def is_expired(self):
        return self.end_date and self.end_date <= timezone.datetime.today().date()

    @property
    def full_name(self):
        return self.name

    def get_users(self, role=None):
        if isinstance(role, str):
            if role not in SYSTEM_PROJECT_ROLES:
                role = get_new_role_name(Project, role)
        project_users = get_project_users(self.id, role)
        return (
            get_user_model().objects.filter(id__in=project_users).order_by("username")
        )

    @transaction.atomic()
    def _soft_delete(self, using=None):
        """Method for project soft delete. It doesn't delete a project, only mark as 'removed', but it sends signals"""
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
        return f"{self.name} | {self.customer.name}"

    def get_log_fields(self):
        return ("uuid", "customer", "name", "end_date")

    def get_parents(self):
        return [self.customer]

    class Meta:
        base_manager_name = "objects"


class CustomerPermissionReview(core_models.UuidMixin):
    class Permissions:
        customer_path = "customer"

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("organization"),
        related_name="reviews",
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
        return "customer_permission_review"

    def close(self, user):
        self.is_pending = False
        self.closed = timezone.now()
        self.reviewer = user
        self.save()


def build_service_settings_query(user):
    return Q(shared=True) | Q(
        shared=False,
        customer__in=get_visible_customers(user),
        is_active=True,
    )


class ServiceSettings(
    quotas_models.ExtendableQuotaModelMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.StateMixin,
    StructureLoggableMixin,
):
    class Meta:
        verbose_name = "Service settings"
        verbose_name_plural = "Service settings"
        ordering = ("name",)

    class Permissions:
        customer_path = "customer"
        build_query = build_service_settings_query

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Customer,
        verbose_name=_("organization"),
        related_name="service_settings",
        blank=True,
        null=True,
    )
    backend_url = core_fields.BackendURLField(max_length=200, blank=True, null=True)
    username = models.CharField(max_length=100, blank=True, null=True)
    password = models.CharField(max_length=100, blank=True, null=True)
    domain = models.CharField(max_length=200, blank=True, null=True)
    token = models.CharField(max_length=255, blank=True, null=True)
    certificate = models.FileField(
        upload_to="certs", blank=True, null=True, validators=[CertificateValidator]
    )
    type = models.CharField(
        max_length=255, db_index=True, validators=[validate_service_type]
    )
    options = JSONField(default=dict, help_text=_("Extra options"), blank=True)
    shared = models.BooleanField(default=False, help_text=_("Anybody can use it"))
    terms_of_services = models.URLField(max_length=255, blank=True)

    tracker = FieldTracker()

    # service settings scope - VM that contains service
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey("content_type", "object_id")

    objects = ServiceSettingsManager("scope")

    is_active = models.BooleanField(
        default=True,
        help_text="Information about inactive service settings will not be updated in the background",
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
        return f"{self.name} ({self.type})"

    def get_log_fields(self):
        return ("uuid", "name", "customer")

    def _get_log_context(self, entity_name):
        context = super()._get_log_context(entity_name)
        context["service_settings_type"] = self.type
        return context

    def get_type_display(self):
        return self.type


class SharedServiceSettings(ServiceSettings):
    """Required for a clear separation of shared/private service settings on admin."""

    objects = SharedServiceSettingsManager()

    class Meta:
        proxy = True
        verbose_name_plural = _("Shared provider settings")


class PrivateServiceSettings(ServiceSettings):
    """Required for a clear separation of shared/private service settings on admin."""

    objects = PrivateServiceSettingsManager()

    class Meta:
        proxy = True
        verbose_name_plural = _("Private provider settings")


class BaseServiceProperty(
    core_models.BackendModelMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    models.Model,
):
    """Base service properties like image, flavor, region,
    which are usually used for Resource provisioning.
    """

    class Meta:
        abstract = True

    @classmethod
    def get_url_name(cls):
        """This name will be used by generic relationships to membership model for URL creation"""
        return f"{cls._meta.app_label}-{cls.__name__.lower()}"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "backend_id",
            "name",
        )


class ServiceProperty(BaseServiceProperty):
    class Meta:
        abstract = True
        unique_together = ("settings", "backend_id")

    settings = models.ForeignKey(
        on_delete=models.CASCADE, to=ServiceSettings, related_name="+"
    )
    backend_id = models.CharField(max_length=255, db_index=True)

    def __str__(self):
        return f"{self.name} | {self.settings}"


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
    TimeStampedModel,
):
    """Base resource class. Resource is a provisioned entity of a service,
    for example: a VM in OpenStack or AWS, or a repository in Github.
    """

    class Meta:
        abstract = True
        ordering = ["-created"]

    class Permissions:
        customer_path = "project__customer"
        project_path = "project"

    service_settings = models.ForeignKey(
        on_delete=models.CASCADE, to=ServiceSettings, related_name="+"
    )
    project = models.ForeignKey(on_delete=models.CASCADE, to=Project, related_name="+")
    backend_id = models.CharField(max_length=255, blank=True)

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("backend_id",)

    def get_backend(self, **kwargs):
        return self.service_settings.get_backend(**kwargs)

    def get_access_url(self):
        # default behaviour. Override in subclasses if applicable
        return None

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @classmethod
    def get_url_name(cls):
        """This name will be used by generic relationships to membership model for URL creation"""
        return f"{cls._meta.app_label}-{cls.__name__.lower()}"

    def get_log_fields(self):
        return ("uuid", "name", "service_settings", "project", "full_name")

    @property
    def full_name(self):
        return "{} {}".format(
            get_resource_type(self).replace(".", " "),
            self.name,
        )

    def _get_log_context(self, entity_name):
        context = super()._get_log_context(entity_name)
        # XXX: Add resource_full_name here, because event context does not support properties as fields
        context["resource_full_name"] = self.full_name
        context["resource_type"] = get_resource_type(self)

        return context

    def get_parents(self):
        return [self.service_settings, self.project]

    def __str__(self):
        return self.name

    def increase_backend_quotas_usage(self, validate=False):
        """Increase usage of quotas that were consumed by resource on creation."""
        pass

    def decrease_backend_quotas_usage(self):
        """Decrease usage of quotas that were released on resource deletion"""
        pass

    @classmethod
    def get_scope_type(cls):
        return get_resource_type(cls)

    @property
    def customer(self):
        return self.project.customer


class VirtualMachine(IPCoordinatesMixin, core_models.RuntimeStateMixin, BaseResource):
    def __init__(self, *args, **kwargs):
        AbstractFieldTracker().finalize_class(self.__class__, "tracker")
        super().__init__(*args, **kwargs)

    cores = models.PositiveSmallIntegerField(
        default=0, help_text=_("Number of cores in a VM")
    )
    ram = models.PositiveIntegerField(default=0, help_text=_("Memory size in MiB"))
    disk = models.PositiveIntegerField(default=0, help_text=_("Disk size in MiB"))
    min_ram = models.PositiveIntegerField(
        default=0, help_text=_("Minimum memory size in MiB")
    )
    min_disk = models.PositiveIntegerField(
        default=0, help_text=_("Minimum disk size in MiB")
    )

    image_name = models.CharField(max_length=150, blank=True)

    key_name = models.CharField(max_length=50, blank=True)
    key_fingerprint = models.CharField(max_length=47, blank=True)

    user_data = models.TextField(
        blank=True,
        help_text=_("Additional data that will be added to instance on provisioning"),
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
    size = models.PositiveIntegerField(help_text=_("Size in MiB"))

    class Meta:
        abstract = True


class Volume(Storage):
    class Meta:
        abstract = True


class Snapshot(Storage):
    class Meta:
        abstract = True


class SubResource(BaseResource):
    """Resource dependent object that cannot exist without resource."""

    class Meta:
        abstract = True

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]


class UserAgreement(core_models.UuidMixin, LoggableMixin, TimeStampedModel):
    class UserAgreements:
        TOS = "TOS"
        PP = "PP"

        CHOICES = (
            (TOS, "Terms of services"),
            (PP, "Privacy policy"),
        )

    content = models.TextField(blank=True)
    agreement_type = models.CharField(
        max_length=5, choices=UserAgreements.CHOICES, unique=True
    )

    class Meta:
        ordering = ["created"]

    def __str__(self):
        return self.agreement_type


reversion.register(Customer)


ROLE_MAP = {
    ("customer", "owner"): RoleEnum.CUSTOMER_OWNER,
    ("customer", "service_manager"): RoleEnum.CUSTOMER_MANAGER,
    ("customer", "support"): RoleEnum.CUSTOMER_SUPPORT,
    ("project", "admin"): RoleEnum.PROJECT_ADMIN,
    ("project", "manager"): RoleEnum.PROJECT_MANAGER,
    ("project", "member"): RoleEnum.PROJECT_MEMBER,
    ("offering", None): RoleEnum.OFFERING_MANAGER,
}


def get_new_role_name(type, old_role_name):
    return ROLE_MAP.get((type._meta.model_name, old_role_name)) or old_role_name


def get_old_role_name(new_role_name):
    keys = [key for key, value in ROLE_MAP.items() if value == new_role_name]
    if keys:
        return keys[0][1]
