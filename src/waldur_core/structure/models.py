import re
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from typing import cast

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models, transaction
from django.db.models import Model, Q, signals
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.fields import AutoCreatedField
from model_utils.managers import SoftDeletableManagerMixin
from model_utils.models import SoftDeletableModel, TimeStampedModel
from model_utils.tracker import FieldInstanceTracker
from netfields import CidrAddressField, NetManager
from reversion import revisions as reversion

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.core import fields as core_fields
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core.enums import ReviewStates
from waldur_core.core.fields import COUNTRIES_DICT, JSONField
from waldur_core.core.models import User
from waldur_core.core.validators import (
    validate_cidr_list,
    validate_name,
    validate_phone_number,
)
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.media.mixins import ImageModelMixin
from waldur_core.media.validators import (
    CertificateValidator,
    validate_notification_emails,
)
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.mixins import PermissionMixin
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.managers import (
    PrivateServiceSettingsManager,
    ServiceSettingsManager,
    SharedServiceSettingsManager,
    filter_queryset_for_user,
    get_connected_customers,
    get_customer_users,
    get_nested_customer_users,
    get_project_users,
    get_visible_customers,
)
from waldur_core.structure.mixins import CoordinatesMixin, IPCoordinatesMixin
from waldur_core.structure.registry import SupportedServices, get_resource_type


def validate_service_type(service_type):
    """
    Validate that a service type is supported.

    Checks against the SupportedServices registry to ensure the service type
    is valid and supported by the system.

    Args:
        service_type: String representing the service type to validate

    Raises:
        ValidationError: If the service type is not supported
    """
    if not SupportedServices.has_service_type(service_type):
        raise ValidationError(_("Invalid service type."))  # type: ignore


class StructureLoggableMixin(LoggableMixin):
    """
    Extends LoggableMixin with structure-specific permission filtering.

    Provides permission filtering for logging operations based on
    structure-specific user permissions and visibility rules.
    """

    @classmethod
    def get_permitted_objects(cls, user):
        return filter_queryset_for_user(cls.objects.all(), user)  # type: ignore


class VATException(Exception):
    """
    Custom exception for VAT-related errors.

    Raised when VAT calculations or validations fail,
    such as missing country codes or VAT service issues.
    """

    pass


class VATMixin(models.Model):
    """
    Add country and VAT number fields for tax compliance and record keeping.
    VAT validation is optional and can be done manually or through external services.
    """

    class Meta:
        abstract = True

    vat_code = models.CharField(max_length=20, blank=True, help_text=_("VAT number"))
    vat_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Optional business name for the VAT number."),
    )
    vat_address = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Optional business address for the VAT number."),
    )

    country = models.CharField(max_length=2, blank=True)

    def get_country_display(self) -> str | None:
        return COUNTRIES_DICT.get(self.country)

    @staticmethod
    def validate_vat_format(vat_code: str, country: str | None = None) -> bool:
        """
        Basic VAT number format validation using regex patterns.
        Returns True if format appears valid, False otherwise.
        Note: This only checks format, not validity with tax authorities.
        """
        if not vat_code:
            return False

        # Remove spaces and dashes, then convert to uppercase
        vat_code = vat_code.replace(" ", "").replace("-", "").upper()

        # European VAT number patterns extracted from regex documentation
        vat_patterns = {
            # EU Countries
            "AT": r"^ATU\d{8}$",  # Austria
            "BE": r"^BE[01]\d{9}$",  # Belgium - updated format
            "BG": r"^BG\d{9,10}$",  # Bulgaria
            "HR": r"^HR\d{11}$",  # Croatia
            "CY": r"^CY\d{8}L$",  # Cyprus
            "CZ": r"^CZ\d{8,10}$",  # Czech Republic
            "DK": r"^DK\d{8}$",  # Denmark
            "EE": r"^EE\d{9}$",  # Estonia
            "FI": r"^FI\d{8}$",  # Finland
            "FR": r"^FR[A-HJ-NP-Z0-9]{2}\d{9}$",  # France - updated format
            "DE": r"^DE\d{9}$",  # Germany
            "EL": r"^EL\d{9}$",  # Greece
            "HU": r"^HU\d{8}$",  # Hungary
            "IE": r"^IE\d{7}[A-WY][A-I]?$|^IE[0-9+][A-Z+][0-9]{5}[A-WY]$",  # Ireland
            "IT": r"^IT\d{11}$",  # Italy
            "LV": r"^LV\d{11}$",  # Latvia
            "LT": r"^LT\d{9,12}$",  # Lithuania
            "LU": r"^LU\d{8}$",  # Luxembourg
            "MT": r"^MT\d{8}$",  # Malta
            "NL": r"^NL\d{9}B\d{2}$",  # Netherlands
            "PL": r"^PL\d{10}$",  # Poland
            "PT": r"^PT\d{9}$",  # Portugal
            "RO": r"^RO\d{2,10}$",  # Romania
            "SK": r"^SK\d{10}$",  # Slovakia
            "SI": r"^SI\d{8}$",  # Slovenia
            "ES": r"^ES[A-Z]\d{7}[A-Z]$|^ES[A-Z][0-9]{7}[0-9A-Z]$|^ES[0-9]{8}[A-Z]$",  # Spain
            "SE": r"^SE\d{12}$",  # Sweden
            # Post-Brexit UK
            "GB": r"^GB\d{9}$|^GB\d{12}$|^GBGD\d{3}$|^GBHA\d{3}$",  # United Kingdom
            # Non-EU European Countries
            "AL": r"^ALJ\d{8}[A-Z]$",  # Albania
            "BY": r"^BY\d{9}$",  # Belarus
            "CH": r"^CHE\d{9}(?:MWST|TVA|IVA)$",  # Switzerland
            "FO": r"^FO\d{6}$",  # Faroe Islands
            "IS": r"^IS\d{5,6}$",  # Iceland
            "LI": r"^LI\d{5}$",  # Liechtenstein
            "MD": r"^MD\d{8}$",  # Moldova
            "ME": r"^ME\d{8}$",  # Montenegro
            "MK": r"^MK\d{13}$",  # North Macedonia
            "NO": r"^NO\d{9}MVA$",  # Norway
            "RS": r"^RS\d{9}$",  # Serbia
            "SM": r"^SM\d{5}$",  # San Marino
            "UA": r"^UA\d{12}$",  # Ukraine
            "XK": r"^XK\d{8}[A-Z]$",  # Kosovo
        }

        # If country is provided, check specific pattern
        if country and country in vat_patterns:
            pattern = vat_patterns[country]
            return bool(re.match(pattern, vat_code))

        # If no country or country not in list, check if it starts with any known prefix
        for country_code, pattern in vat_patterns.items():
            if vat_code.startswith(country_code) and re.match(pattern, vat_code):
                return True

        # Generic fallback - at least 2 letter country code and some digits
        return bool(re.match(r"^[A-Z]{2}[A-Z0-9]{6,}$", vat_code))


class BasePermission(models.Model):
    """
    Abstract base class for permission models.

    Provides common fields and functionality for permission tracking
    including user, creator, creation time, expiration, and active status.
    Used as a base for specific permission types throughout the system.
    """

    class Meta:
        abstract = True

    user = models.ForeignKey[User](
        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL, db_index=True
    )
    created_by = models.ForeignKey[User](
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


class OrganizationGroup(core_models.UuidMixin, core_models.NameMixin, models.Model):
    """
    Hierarchical organization grouping model.

    Provides tree structure for organizing customers with parent-child
    relationships. Allows for hierarchical organization of customers
    into groups and sub-groups.
    """

    customers: models.Manager["Customer"]

    parent = models.ForeignKey["OrganizationGroup"](
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


class AffiliatedOrganization(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
):
    """
    External organization that projects can be affiliated with.

    Provides a flat registry of organizations (separate from Customer)
    for cross-project reporting and filtering by organizational affiliation.
    Staff-managed; any authenticated user can read.
    """

    code = models.CharField(
        max_length=20,
        unique=True,
        help_text=_("Unique short identifier, e.g. CERN, EMBL."),
    )
    abbreviation = models.CharField(max_length=12, blank=True)
    email = models.EmailField(max_length=75, blank=True)
    homepage = models.URLField(max_length=255, blank=True)
    country = models.CharField(max_length=2, blank=True)
    address = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = _("affiliated organization")
        ordering = ("name",)

    @classmethod
    def get_url_name(cls):
        return "affiliated-organization"

    def __str__(self):
        return self.name


CUSTOMER_DETAILS_FIELDS = (
    "name",
    "slug",
    "native_name",
    "abbreviation",
    "description",
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
    "latitude",
    "longitude",
    "bank_account",
    "country",
    "notification_emails",
    "city",
    "state",
    "parish",
    "street",
    "house_nr",
    "apartment_nr",
    "household",
)


def validate_cidr_32(value):
    """
    Validate that a CIDR address has a /32 mask.

    Ensures that the provided CIDR address uses a /32 subnet mask,
    which represents a single IP address.

    Args:
        value: CIDR address string to validate

    Raises:
        ValidationError: If the CIDR address doesn't have a /32 mask
    """
    if not value.endswith("/32"):
        raise ValidationError("Only /32 mask is allowed.")


class AccessSubnet(core_models.UuidMixin, core_models.DescribableMixin, LoggableMixin):
    """
    Model for customer access subnets.

    Stores CIDR addresses with /32 mask validation for IP-based access control.
    Used to restrict access to customer resources based on source IP addresses.
    """

    customer = models.ForeignKey["Customer"](
        on_delete=models.CASCADE, to="Customer", related_name="access_subnet_set"
    )
    inet = CidrAddressField(null=True, blank=True, validators=[validate_cidr_32])
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta:
        unique_together = ("customer", "inet")
        ordering = ["inet"]

    def __str__(self):
        return self.customer.name + " | " + str(self.inet)

    def get_log_fields(self):
        return "description", "inet", "customer"


class CustomerAddressDetailsMixin(models.Model):
    """
    Mixin contains customer address detail fields.
    """

    class Meta:
        abstract = True

    address = models.CharField(blank=True, max_length=300)
    contact_details = models.TextField(blank=True, validators=[MaxLengthValidator(500)])
    city = models.CharField(blank=True, max_length=100)
    state = models.CharField(blank=True, max_length=100)
    parish = models.CharField(blank=True, max_length=100)
    street = models.CharField(blank=True, max_length=200)
    house_nr = models.CharField(blank=True, max_length=100)
    apartment_nr = models.CharField(blank=True, max_length=100)
    household = models.CharField(blank=True, max_length=100)


class CustomerDetailsMixin(
    core_models.NameMixin, VATMixin, CoordinatesMixin, CustomerAddressDetailsMixin
):
    """
    Mixin containing customer detail fields.

    Provides comprehensive customer information fields including
    native name, contact details, agreement number, email, phone,
    address, banking information, and external system integration.
    """

    class Meta:
        abstract = True

    native_name = models.CharField(max_length=160, default="", blank=True)
    abbreviation = models.CharField(max_length=12, blank=True)

    agreement_number = models.CharField(max_length=160, default="", blank=True)
    sponsor_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("External ID of the sponsor covering the costs"),
    )

    email = models.EmailField(_("email address"), max_length=75, blank=True)
    phone_number = models.CharField(
        _("phone number"),
        max_length=255,
        blank=True,
        validators=[validate_phone_number],
    )
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

    postal = models.CharField(blank=True, max_length=20)
    bank_name = models.CharField(blank=True, max_length=150)
    bank_account = models.CharField(blank=True, max_length=50)
    notification_emails = models.CharField(
        blank=True,
        default="",
        help_text=_("Comma-separated list of notification email addresses"),
        max_length=640,
        validators=[validate_notification_emails],
    )


class ServiceAccountMixin(models.Model):
    """
    Mixin for models that support service accounts.

    Provides functionality for managing service accounts with
    configurable maximum limits. Used by customers and projects
    to control service account creation.
    """

    class Meta:
        abstract = True

    max_service_accounts = models.PositiveSmallIntegerField(
        default=0,
        help_text=_("Maximum number of service accounts allowed"),
        null=True,
        blank=True,
    )


def filter_customers(user):
    """
    Filter customers based on user permissions for call managing organizations.

    Returns a Q object that filters customers based on the user's connection
    to call managing organizations, either directly or through calls.

    Args:
        user: User object to filter customers for

    Returns:
        Q object for filtering customers
    """
    from waldur_mastermind.proposal.managers import (
        get_connected_call_organizers,
        get_connected_calls,
    )

    return Q(callmanagingorganisation__in=get_connected_call_organizers(user)) | Q(
        callmanagingorganisation__call__in=get_connected_calls(user)
    )


class Customer(
    CustomerDetailsMixin,
    core_models.UuidMixin,
    core_models.DescribableMixin,
    core_models.DescendantMixin,
    core_models.SlugMixin,
    core_models.UserDetailsMatchMixin,
    quotas_models.ExtendableQuotaModelMixin,
    PermissionMixin,
    StructureLoggableMixin,
    ImageModelMixin,
    ServiceAccountMixin,
    TimeStampedModel,
):
    """
    Main organization model.

    Represents an organization/customer with comprehensive functionality
    including accounting, tax management, blocking, archiving, and
    organization group relationships. Provides user management,
    billing functionality, and quota tracking.
    """

    access_subnet_set: models.Manager["AccessSubnet"]
    projects: models.Manager["Project"]
    reviews: models.Manager["CustomerPermissionReview"]
    service_settings: models.Manager["ServiceSettings"]
    id: int

    class Permissions:
        customer_path = "self"
        project_path = "projects"
        build_query = filter_customers

    accounting_start_date = models.DateTimeField(
        _("Start date of accounting"), default=timezone.now
    )
    default_tax_percent = models.DecimalField(
        default=Decimal(0),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal(0)), MaxValueValidator(Decimal(200))],
    )
    blocked = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    display_billing_info_in_projects = models.BooleanField(default=True)
    organization_groups = models.ManyToManyField(
        to=OrganizationGroup, related_name="customers", blank=True
    )
    project_metadata_checklist = models.ForeignKey(
        checklist_models.Checklist,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=_("Checklist used for project metadata collection"),
    )
    grace_period_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Number of extra days after project end date before resources are terminated"
        ),
    )
    tracker = cast(
        FieldInstanceTracker, FieldTracker(fields=["project_metadata_checklist"])
    )
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
        return ("uuid", "name", "abbreviation", "description", "contact_details")

    def clean(self):
        """Validate that the project metadata checklist is of PROJECT_METADATA type."""
        super().clean()
        if self.project_metadata_checklist:
            if (
                self.project_metadata_checklist.checklist_type
                != ChecklistTypes.PROJECT_METADATA
            ):
                raise ValidationError(
                    {
                        "project_metadata_checklist": _(
                            "Checklist must be of type PROJECT_METADATA"
                        )
                    }
                )

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
            return User.objects.filter(id__in=users)

        return (
            User.objects.filter(id__in=get_nested_customer_users(self))
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


class ProjectType(
    core_models.DescribableMixin, core_models.UuidMixin, core_models.NameMixin
):
    """
    Simple model for categorizing projects.

    Provides project type classification with name, description,
    and UUID identification. Used to categorize projects into
    different types for organizational purposes.
    """

    class Meta:
        verbose_name = _("Project type")
        verbose_name_plural = _("Project types")
        ordering = ["name"]

    @classmethod
    def get_url_name(cls):
        return "project_type"

    def __str__(self):
        return self.name


class SoftDeletableManager(SoftDeletableManagerMixin, models.Manager):
    """
    Manager for soft-deletable models.

    Provides management for models that can be marked as deleted
    without actual removal from the database. Combines soft deletion
    functionality with standard Django model management.
    """

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
    """
    Mixin providing OECD FOS 2007 classification codes for research projects.

    Provides standardized classification codes for different scientific fields
    according to the OECD Fields of Science and Technology (FOS) 2007 standard.
    Used to categorize research projects by their scientific domain.
    """

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


class Project(
    core_models.DescribableMixin,
    ProjectOECDFOS2007CodeMixin,
    core_models.UuidMixin,
    core_models.DescendantMixin,
    core_models.BackendMixin,
    core_models.SlugMixin,
    core_models.UserDetailsMatchMixin,
    quotas_models.ExtendableQuotaModelMixin,
    PermissionMixin,
    StructureLoggableMixin,
    ImageModelMixin,
    ServiceAccountMixin,
    TimeStampedModel,
    SoftDeletableModel,
):
    """
    Project model with comprehensive functionality.

    Supports soft deletion, OECD classification, start/end dates,
    and customer relationships. Provides quota management, user
    permissions, and resource organization within customers.
    """

    class Permissions:
        customer_path = "customer"
        project_path = "self"
        list_permission = PermissionEnum.LIST_PROJECTS

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        enable_fields_caching = False
        nc_resource_count = quotas_fields.CounterQuotaField(
            target_models=lambda: BaseResource.get_all_models(),
            path_to_scope="project",
        )

    # NameMixin is not used because it has too strict limitation for max_length.
    name = models.CharField(
        _("name"), max_length=PROJECT_NAME_LENGTH, validators=[validate_name]
    )

    start_date = models.DateField(null=True, blank=True)

    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_(
            "The date is inclusive. Once reached, all project resource will be scheduled for termination."
        ),
    )
    end_date_requested_by = models.ForeignKey[core_models.User](
        on_delete=models.SET_NULL,
        to=core_models.User,
        blank=True,
        null=True,
        related_name="+",
    )
    end_date_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp of the last end_date change."),
    )
    type = models.ForeignKey(
        ProjectType,
        verbose_name=_("project type"),
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )
    is_industry = models.BooleanField(default=False)

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("organization"),
        related_name="projects",
        on_delete=models.CASCADE,
    )

    kind = models.CharField(
        max_length=255,
        default=ProjectKind.DEFAULT,
        choices=ProjectKind.choices,
        verbose_name=_("project type"),
    )

    termination_metadata = JSONField(
        null=True,
        blank=True,
        help_text=_("Metadata captured at project termination for recovery purposes"),
    )

    staff_notes = models.CharField(
        _("staff notes"),
        max_length=core_models.DESCRIPTION_LENGTH,
        blank=True,
        help_text=_("Internal notes visible only to staff and support users"),
    )
    grace_period_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Number of extra days after project end date before resources are terminated. Overrides customer-level setting."
        ),
    )
    affiliated_organizations = models.ManyToManyField(
        to=AffiliatedOrganization,
        related_name="projects",
        blank=True,
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())
    # Entities returned in manager available_objects are limited to not-deleted instances.
    # Entities returned in manager objects include deleted objects.
    available_objects = SoftDeletableManager()
    objects = models.Manager()
    id: int

    def save(self, *args, **kwargs):
        end_date_changed = not self._state.adding and self.tracker.has_changed(
            "end_date"
        )
        super().save(*args, **kwargs)
        if end_date_changed:
            Project.objects.filter(pk=self.pk).update(
                end_date_updated_at=timezone.now()
            )

    def get_grace_period_days(self):
        """Get the grace period days, with project-level setting overriding customer-level."""
        if self.grace_period_days is not None:
            return self.grace_period_days
        if self.customer.grace_period_days is not None:
            return self.customer.grace_period_days
        return 0

    def get_effective_end_date(self):
        """Get the effective end date including grace period."""
        if not self.end_date:
            return None
        grace_days = self.get_grace_period_days()
        return self.end_date + timedelta(days=grace_days)

    @property
    def is_expired(self):
        effective_end_date = self.get_effective_end_date()
        return effective_end_date and effective_end_date <= timezone.now().date()

    @property
    def is_in_grace_period(self):
        """Check if project is currently in grace period (past end_date but before effective_end_date)."""
        if not self.end_date:
            return False

        today = timezone.now().date()
        effective_end_date = self.get_effective_end_date()

        # In grace period if past end_date but before effective_end_date
        return (
            self.end_date < today and effective_end_date and today <= effective_end_date
        )

    @property
    def end_date_with_grace(self):
        """Get the end date including grace period (same as get_effective_end_date but as property)."""
        return self.get_effective_end_date()

    @property
    def full_name(self) -> str:
        return self.name

    def get_users(self, role=None):
        project_users = get_project_users(self.id, role)
        return User.objects.filter(id__in=project_users).order_by("username")

    @transaction.atomic()
    def _soft_delete(self, using=None, terminated_by=None):
        """Method for project soft delete. It doesn't delete a project, only mark as 'removed', but it sends signals"""
        # Store terminating user for handler access
        if terminated_by:
            self._terminating_user = terminated_by

        # Set flag to indicate this object is being deleted to skip quota aggregation
        self._deleting = True

        signals.pre_delete.send(sender=self.__class__, instance=self, using=using)

        self.is_removed = True
        self.save(using=using)

        signals.post_delete.send(sender=self.__class__, instance=self, using=using)

    def delete(self, using=None, soft=True, *args, **kwargs):
        """Use soft delete, i.e. mark a project as 'removed'."""
        if soft:
            self._soft_delete(using)
        else:
            # Set flag for hard delete as well
            self._deleting = True
            return super(SoftDeletableModel, self).delete(using=using, *args, **kwargs)

    def __str__(self):
        return f"{self.name} | {self.customer.name}"

    def get_log_fields(self):
        return ("uuid", "customer", "name", "end_date")

    def get_parents(self):
        try:
            # Check if customer relationship exists and return it
            if hasattr(self, "customer") and self.customer:
                return [self.customer]
            return []
        except (AttributeError, KeyError):
            # Handle case where customer relationship is missing during deletion
            return []

    class Meta:
        base_manager_name = "objects"
        ordering = ["name"]


class PermissionReview(core_models.UuidMixin, LoggableMixin):
    """
    Abstract base class for permission reviews.

    Tracks permission review processes with pending status,
    creation/closure dates, and reviewer information.
    Used for auditing and managing access permissions.
    """

    class Meta:
        abstract = True

    is_pending = models.BooleanField(default=True)
    created = AutoCreatedField()
    closed = models.DateTimeField(null=True, blank=True)
    reviewer = models.ForeignKey[User](
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    def close(self, user):
        self.is_pending = False
        self.closed = timezone.now()
        self.reviewer = user
        self.save()


class CustomerPermissionReview(PermissionReview):
    """
    Model for tracking customer permission reviews.

    Inherits from PermissionReview and adds customer-specific fields.
    """

    class Permissions:
        customer_path = "customer"
        list_permission = PermissionEnum.LIST_CUSTOMER_PERMISSION_REVIEWS

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("organization"),
        related_name="reviews",
        on_delete=models.CASCADE,
    )

    @classmethod
    def get_url_name(cls):
        return "customer_permission_review"


class ProjectPermissionReview(PermissionReview):
    """
    Model for tracking project permission reviews.

    Inherits from PermissionReview and adds project-specific fields.
    """

    class Permissions:
        customer_path = "project__customer"
        project_path = "project"
        list_permission = PermissionEnum.REVIEW_PROJECT_MEMBERSHIP

    project = models.ForeignKey(
        Project,
        verbose_name=_("project"),
        related_name="reviews",
        on_delete=models.CASCADE,
    )


def build_service_settings_query(user):
    """
    Build query for service settings based on sharing and customer visibility.

    Creates a Q object that includes shared service settings or private
    service settings that belong to customers visible to the user.

    Args:
        user: User object to build query for

    Returns:
        Q object for filtering service settings
    """
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
    """
    Configuration model for external service integrations.

    Stores authentication credentials, options, and backend connection
    details for external services. Supports both shared and private
    configurations with quota management and state tracking.
    """

    class Meta:
        verbose_name = "Service settings"
        verbose_name_plural = "Service settings"
        ordering = ("name",)

    class Permissions:
        customer_path = "customer"
        build_query = build_service_settings_query

    id: int
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

    tracker = cast(FieldInstanceTracker, FieldTracker())

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
        options = cast(dict, self.options) or {}
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
    """
    Proxy model for shared service configurations.

    Required for clear separation of shared/private service settings
    in the admin interface. Provides specialized management for
    service settings that can be used by multiple customers.
    """

    objects = SharedServiceSettingsManager()

    class Meta:
        proxy = True
        verbose_name_plural = _("Shared provider settings")


class PrivateServiceSettings(ServiceSettings):
    """
    Proxy model for private service configurations.

    Required for clear separation of shared/private service settings
    in the admin interface. Provides specialized management for
    service settings that belong to specific customers.
    """

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
    """
    Abstract base class for service properties.

    Provides common functionality for service properties like images,
    flavors, and regions that are typically used for resource provisioning.
    Includes backend integration and URL naming capabilities.
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
    """
    Service property model connected to specific service settings.

    Extends BaseServiceProperty with service settings relationship
    and backend ID uniqueness constraint. Used for properties that
    are specific to particular service configurations.
    """

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
    Service property model not connected to specific settings.

    Provides service properties that are independent of specific
    service settings, with unique backend ID constraint.
    Used for global or shared service properties.
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
    """
    Abstract base class for all provisioned resources.

    Represents a provisioned entity of a service such as a VM in OpenStack
    or AWS, or a repository in GitHub. Provides common functionality for
    all resource types including service settings, project relationships,
    backend integration, and state management.
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
    def get_all_models(cls) -> list[type[Model]]:
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @classmethod
    def get_url_name(cls):
        """This name will be used by generic relationships to membership model for URL creation"""
        return f"{cls._meta.app_label}-{cls.__name__.lower()}"

    def get_log_fields(self):
        return ("uuid", "name", "service_settings", "project", "full_name")

    @property
    def full_name(self) -> str:
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
    def get_scope_type(cls) -> str:
        return get_resource_type(cls)

    @property
    def customer(self) -> Customer:
        return self.project.customer

    @property
    def marketplace_uuid(self) -> str | None:
        from waldur_mastermind.marketplace.models import Resource

        try:
            return Resource.objects.get(scope=self).uuid.hex
        except (Resource.DoesNotExist, Resource.MultipleObjectsReturned):
            return None


class VirtualMachine(IPCoordinatesMixin, core_models.RuntimeStateMixin, BaseResource):
    """
    Abstract model for virtual machines.

    Provides VM-specific functionality including CPU, RAM, disk specifications,
    IP address handling, and runtime state management. Defines common interface
    for all VM implementations across different cloud providers.
    """

    class Meta:
        abstract = True

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


class Storage(core_models.RuntimeStateMixin, BaseResource):
    """
    Abstract model for storage resources.

    Provides storage-specific functionality with size specification
    and runtime state management. Used as base class for all
    storage resource implementations.
    """

    size = models.PositiveIntegerField(help_text=_("Size in MiB"))

    class Meta:
        abstract = True


class UserAgreement(core_models.UuidMixin, LoggableMixin, TimeStampedModel):
    """
    Model for storing user agreements.

    Stores different types of user agreements such as Terms of Service
    and Privacy Policy with content and agreement type classification.
    Used for legal compliance and user consent tracking.

    Supports multiple language versions per agreement type.
    If language is empty, it's the default/fallback version.
    """

    class UserAgreements:
        TOS = "TOS"
        PP = "PP"

        CHOICES = (
            (TOS, "Terms of services"),
            (PP, "Privacy policy"),
        )

    content = models.TextField(blank=True)
    agreement_type = models.CharField(
        max_length=5,
        choices=UserAgreements.CHOICES,
    )
    language = models.CharField(
        max_length=10,
        blank=True,
        help_text="ISO 639-1 language code (e.g., 'en', 'de', 'et'). "
        "Leave empty for the default version.",
    )

    class Meta:
        ordering = ["created"]
        unique_together = [("agreement_type", "language")]

    def __str__(self):
        if self.language:
            return f"{self.agreement_type} ({self.language})"
        return f"{self.agreement_type} (default)"


reversion.register(Customer)


class ExternalLink(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    ImageModelMixin,
    TimeStampedModel,
):
    """
    Model for external links associated with waldur.

    Provides a way to store URLs with names and descriptions for
    external systems.
    """

    class Meta:
        verbose_name = _("External link")
        verbose_name_plural = _("External links")
        ordering = ("name",)

    link = models.URLField(max_length=500)


class ProjectDigestConfiguration(
    core_models.UuidMixin,
    TimeStampedModel,
):
    """Organization-level configuration for periodic project member digest emails."""

    class Frequency(models.TextChoices):
        WEEKLY = "weekly", _("Weekly")
        BIWEEKLY = "biweekly", _("Bi-weekly")
        MONTHLY = "monthly", _("Monthly")

    customer = models.OneToOneField(
        "structure.Customer",
        on_delete=models.CASCADE,
        related_name="project_digest_config",
    )
    is_enabled = models.BooleanField(default=False)
    frequency = models.CharField(
        max_length=20,
        choices=Frequency.choices,
        default=Frequency.MONTHLY,
    )
    enabled_sections = JSONField(
        default=list,
        blank=True,
        help_text=_("List of section keys to include. Empty means all."),
    )
    last_sent_at = models.DateTimeField(null=True, blank=True)
    day_of_week = models.PositiveSmallIntegerField(
        default=1,
        validators=[MaxValueValidator(6)],
        help_text=_("For weekly/biweekly: 0=Sunday..6=Saturday"),
    )
    day_of_month = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text=_("For monthly: day of month (1-28)"),
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("Project digest configuration")
        verbose_name_plural = _("Project digest configurations")

    def __str__(self):
        return f"Digest config for {self.customer} ({self.frequency})"


class ProjectEndDateChangeRequest(core_models.UuidMixin, core_mixins.ReviewMixin):
    """
    Request from project member (without UPDATE_PROJECT) to change project end date.
    Organization owners can approve or reject.
    """

    class Meta:
        ordering = ["created"]
        verbose_name = _("Project end date change request")
        verbose_name_plural = _("Project end date change requests")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "requested_end_date"],
                condition=Q(state=ReviewStates.PENDING),
                name="unique_pending_request_per_project_date",
            )
        ]

    tracker = cast(FieldInstanceTracker, FieldTracker())
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="end_date_change_requests",
    )
    requested_end_date = models.DateField(
        help_text=_("The requested new end date for the project"),
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
    )
    comment = models.TextField(
        blank=True,
        null=True,
        help_text=_("Optional comment from the requester"),
    )

    class Permissions:
        customer_path = "project__customer"
        project_path = "project"
