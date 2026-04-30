import datetime
import logging
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from constance import config as constance_config
from dateutil.relativedelta import relativedelta
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Index, Sum
from django.db.models.constraints import UniqueConstraint
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMField, FSMIntegerField, transition
from model_utils import FieldTracker
from model_utils.models import TimeFramedModel, TimeStampedModel
from model_utils.tracker import FieldInstanceTracker
from rest_framework import exceptions as rf_exceptions
from reversion import revisions as reversion

import waldur_core.media.mixins
from waldur_core.core import fields as core_fields
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core.models import User, generate_slug
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.media.mixins import get_upload_path
from waldur_core.media.validators import FileTypeValidator, ImageValidator
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.mixins import PermissionMixin
from waldur_core.permissions.utils import get_users
from waldur_core.quotas import fields as quotas_fields
from waldur_core.quotas import models as quotas_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.mixins import CoordinatesMixin
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    CategoryColumnWidget,
    CourseAccountState,
    ImpactLevel,
    LimitPeriods,
    MaintenanceState,
    MaintenanceType,
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
    ServiceAccountState,
)
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.notifications import models as notifications_models
from waldur_pid import mixins as pid_mixins

from ..common import mixins as common_mixins
from . import managers, plugins, signals
from .attribute_types import ATTRIBUTE_TYPES

logger = logging.getLogger(__name__)


class ServiceProvider(
    PermissionMixin,
    core_models.UuidMixin,
    core_models.DescribableMixin,
    waldur_core.media.mixins.ImageModelMixin,
    TimeStampedModel,
):
    """
    Service provider model representing organizations that offer services.

    Represents organizations that provide services in the marketplace.
    Includes notification settings, API credentials, and lead contact
    information for order processing.
    """

    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)
    api_secret_code = models.CharField(max_length=255, null=True, blank=True)
    lead_email = models.EmailField(
        null=True,
        blank=True,
        help_text=_(
            "Email for notification about new request based orders. "
            "If this field is set, notifications will be sent."
        ),
    )
    lead_subject = models.CharField(
        max_length=255,
        blank=True,
        help_text=_(
            "Notification subject template. Django template variables can be used."
        ),
    )
    lead_body = models.TextField(
        blank=True,
        help_text=_(
            "Notification body template. Django template variables can be used."
        ),
        validators=[core_validators.validate_template_syntax],
    )
    allowed_domains = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of allowed domains for offering endpoints. "
            "Only staff can modify this field. "
        ),
    )

    class Permissions:
        customer_path = "customer"

    class Meta:
        verbose_name = _("Service provider")

    def __str__(self):
        return str(self.customer)

    @classmethod
    def get_url_name(cls):
        return "marketplace-service-provider"

    @property
    def has_active_offerings(self) -> bool:
        return (
            Offering.objects.filter(customer=self.customer)
            .exclude(state=OfferingStates.ARCHIVED)
            .exists()
        )

    @property
    def offering_count(self) -> int:
        return Offering.objects.filter(
            customer=self.customer,
            state__in=[
                OfferingStates.ACTIVE,
                OfferingStates.PAUSED,
                OfferingStates.UNAVAILABLE,
            ],
        ).count()

    def generate_api_secret_code(self):
        self.api_secret_code = core_utils.pwgen()

    def save(self, *args, **kwargs):
        if not self.pk:
            self.generate_api_secret_code()
        super().save(*args, **kwargs)


class CategoryGroup(
    core_models.UuidMixin,
    TimeStampedModel,
):
    """
    Organizational grouping for categories.

    Provides hierarchical organization of marketplace categories
    with title, icon, and description. Used to group related
    categories for better navigation and organization.
    """

    title = models.CharField(blank=False, max_length=255)
    icon = models.FileField(
        upload_to="marketplace_category_group_icons",
        blank=True,
        null=True,
        validators=[ImageValidator],
    )
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = _("Category group")
        verbose_name_plural = _("Category groups")
        ordering = ("title",)

    def __str__(self):
        return str(self.title)

    @classmethod
    def get_url_name(cls):
        return "marketplace-category-group"


class Category(
    core_models.BackendMixin,
    core_models.UuidMixin,
    quotas_models.QuotaModelMixin,
    TimeStampedModel,
):
    """
    Main category model for marketplace offerings.

    Provides categorization for marketplace offerings with quota management,
    default flags for VM/volume/tenant categories, and backend integration.
    Supports offering organization and filtering in the marketplace.
    """

    id: int
    columns: models.Manager["CategoryColumn"]
    sections: models.Manager["Section"]
    components: models.Manager["CategoryComponent"]
    offerings: models.Manager["Offering"]
    articles: models.Manager["CategoryHelpArticle"]

    title = models.CharField(blank=False, max_length=255)
    icon = models.FileField(
        upload_to="marketplace_category_icons",
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

    group = models.ForeignKey(
        CategoryGroup, blank=True, null=True, on_delete=models.SET_NULL
    )

    def clean_fields(self, exclude=None):
        super().clean_fields(exclude=exclude)

        for flag in [
            "default_volume_category",
            "default_vm_category",
            "default_tenant_category",
        ]:
            if getattr(self, flag):
                category = (
                    Category.objects.filter(**{flag: True}).exclude(id=self.id).first()
                )
                if category:
                    raise ValidationError(
                        {
                            flag: _("%s is already %s.")
                            % (category, flag.replace("_", " ")),
                        }
                    )

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        offering_count = quotas_fields.QuotaField(is_backend=True)

    class Meta:
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        ordering = ("title",)

    def __str__(self):
        return str(self.title)

    @classmethod
    def get_url_name(cls):
        return "marketplace-category"


class CategoryColumn(
    core_models.UuidMixin,
):
    """
    Configuration model for rendering resource tables with custom columns.

    Defines custom columns for resource tables with attribute mapping
    and widget support for UI customization. Allows filtering and
    sorting by resource attributes.
    """

    """
    This model is needed in order to render resources table with extra columns.
    Usually each column corresponds to specific resource attribute.
    However, one table column may correspond to several resource attributes.
    In this case custom widget should be specified.
    If attribute field is specified, it is possible to filter and sort resources by it's value.
    """

    class Meta:
        ordering = ("category", "index")

    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name="columns"
    )
    index = models.PositiveSmallIntegerField(
        help_text=_("Index allows to reorder columns.")
    )
    title = models.CharField(
        blank=False, max_length=255, help_text=_("Title is rendered as column header.")
    )
    attribute = models.CharField(
        blank=True,
        max_length=255,
        help_text=_("Resource attribute is rendered as table cell."),
    )
    widget = models.CharField(
        blank=True,
        null=True,
        max_length=255,
        help_text=_("Widget field allows to customise table cell rendering."),
        choices=CategoryColumnWidget.CHOICES,
    )

    def __str__(self):
        return str(self.title)

    def clean(self):
        if not self.attribute and not self.widget:
            raise ValidationError(
                _("Either attribute or widget field should be specified.")
            )


class Section(TimeStampedModel):
    """
    Organizational section within categories containing related attributes.

    Groups related attributes within a category with support for
    standalone tab rendering. Used for organizing complex attribute
    sets in the marketplace UI.
    """

    attributes: models.Manager["Attribute"]

    key = models.CharField(primary_key=True, max_length=255)
    title = models.CharField(blank=False, max_length=255)
    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name="sections"
    )
    is_standalone = models.BooleanField(
        default=False, help_text=_("Whether section is rendered as a separate tab.")
    )

    def __str__(self):
        return str(self.title)


InternalNameValidator = RegexValidator(
    r"^[a-zA-Z0-9_\-\/:]+$",
    message="Internal name must contain only alphanumeric characters, underscores, hyphens, and slashes.",
)

# Regex validator for internal names allowing alphanumeric characters, underscores, hyphens, and slashes


class Attribute(TimeStampedModel, core_models.UuidMixin):
    """
    Configuration model for category attributes.

    Defines attributes with types, validation rules, and default values.
    Supports various attribute types for dynamic form generation in
    the marketplace interface.
    """

    options: models.Manager["AttributeOption"]

    key = models.CharField(
        primary_key=True, max_length=255, validators=[InternalNameValidator]
    )
    title = models.CharField(blank=False, max_length=255)
    section = models.ForeignKey(
        on_delete=models.CASCADE, to=Section, related_name="attributes"
    )
    type = models.CharField(max_length=255, choices=ATTRIBUTE_TYPES)
    required = models.BooleanField(
        default=False, help_text=_("A value must be provided for the attribute.")
    )
    default = models.JSONField(null=True, blank=True)

    def __str__(self):
        return str(self.title)


class AttributeOption(core_models.UuidMixin, models.Model):
    """
    Options for choice-based attributes.

    Provides key-value pairs for dropdown selections in choice-based
    attributes. Used to define available options for attribute selection.
    """

    attribute = models.ForeignKey(
        Attribute, related_name="options", on_delete=models.CASCADE
    )
    key = models.CharField(max_length=255, validators=[InternalNameValidator])
    title = models.CharField(max_length=255)

    class Meta:
        unique_together = ("attribute", "key")

    def __str__(self):
        return str(self.title)


class BaseComponent(LoggableMixin, core_models.DescribableMixin):
    """
    Abstract base class for measured units.

    Provides common functionality for measured units with name, type,
    and measurement unit fields. Used for billing and usage tracking
    across different component types.
    """

    class Meta:
        abstract = True

    name = models.CharField(
        max_length=150,
        help_text=_("Display name for the measured unit, for example, Floating IP."),
    )
    type = models.CharField(
        max_length=50,
        help_text=_(
            "Unique internal name of the measured unit, for example floating_ip."
        ),
        validators=[InternalNameValidator],
    )
    measured_unit = models.CharField(
        max_length=30, help_text=_("Unit of measurement, for example, GB."), blank=True
    )

    def get_log_fields(self):
        return (
            "name",
            "type",
            "measured_unit",
        )


class CategoryComponent(BaseComponent, core_models.UuidMixin):
    """
    Category-level component definition.

    Defines components at the category level with unique type constraint
    per category. Used as a template for offering components and provides
    base component definitions for categories.
    """

    class Meta:
        unique_together = ("type", "category")

    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name="components"
    )

    def __str__(self):
        return f"{self.name}, category: {self.category.title}"


class CategoryComponentUsage(core_mixins.ScopeMixin):
    """
    Usage tracking for category components.

    Tracks usage of category components with date-based reporting
    and fixed usage override capabilities. Used for monitoring
    and reporting component consumption.
    """

    component = models.ForeignKey(on_delete=models.CASCADE, to=CategoryComponent)
    date = models.DateField()
    reported_usage = models.BigIntegerField(null=True)
    fixed_usage = models.BigIntegerField(null=True)
    objects = managers.MixinManager("scope")

    def __str__(self):
        return f"component: {str(self.component.name)}, date: {self.date}"


def offering_has_plans(offering):
    """
    Validator function checking if offering has associated plans.

    Checks if the offering has plans directly or inherits plans from its parent.
    Used as a condition for offering state transitions.

    Args:
        offering: Offering instance to check

    Returns:
        bool: True if offering has plans, False otherwise
    """
    return offering.plans.count() or (offering.parent and offering.parent.plans.count())


class Offering(
    core_models.BackendMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.SlugMixin,
    core_models.DescribableMixin,
    quotas_models.QuotaModelMixin,
    PermissionMixin,
    TimeStampedModel,
    core_mixins.ScopeMixin,
    LoggableMixin,
    pid_mixins.DataciteMixin,
    CoordinatesMixin,
    waldur_core.media.mixins.ImageModelMixin,
    common_mixins.BackendMetadataMixin,
):
    """
    Central marketplace offering model.

    Provides comprehensive functionality for marketplace offerings including
    state management, pricing, attributes, and integration with external systems.
    Supports different offering types, billing models, and access control.
    """

    children: models.Manager["Offering"]
    components: models.Manager["OfferingComponent"]
    plans: models.Manager["Plan"]
    screenshots: models.Manager["Screenshot"]
    files: models.Manager["OfferingFile"]
    endpoints: models.Manager["OfferingAccessEndpoint"]
    roles: models.Manager["OfferingUserRole"]
    software_catalogs: models.Manager["OfferingSoftwareCatalog"]
    user_consents: models.Manager["UserOfferingConsent"]
    terms_of_service_configs: models.Manager["OfferingTermsOfService"]
    get_state_display: Callable[
        [], Literal["Draft", "Active", "Paused", "Archived", "Unavailable"]
    ]

    class States(OfferingStates):
        pass

    thumbnail = models.FileField(
        upload_to="marketplace_service_offering_thumbnails",
        blank=True,
        null=True,
        validators=[ImageValidator],
    )
    remote_image_uuid = models.UUIDField(null=True, blank=True, default=None)
    full_description = models.TextField(blank=True)
    vendor_details = models.TextField(blank=True)
    getting_started = models.TextField(blank=True)
    integration_guide = models.TextField(blank=True)
    category = models.ForeignKey(
        on_delete=models.CASCADE, to=Category, related_name="offerings"
    )
    compliance_checklist = models.ForeignKey(
        to="checklist.Checklist",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offerings",
        limit_choices_to={"checklist_type": "offering_compliance"},
        help_text=_("Checklist that offering users must complete for compliance"),
    )
    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Customer,
        related_name="+",
        null=True,
    )
    project = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Project,
        related_name="+",
        null=True,
        blank=True,
    )
    # Volume offering is linked with VPC offering via parent field
    parent = models.ForeignKey["Offering"](
        on_delete=models.CASCADE,
        to="Offering",
        null=True,
        blank=True,
        related_name="children",
    )
    attributes = models.JSONField(
        blank=True, default=dict, help_text=_("Fields describing Category.")
    )
    options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_("Fields describing resource provision form."),
    )
    resource_options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_("Fields describing resource report form."),
    )
    plugin_options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            "Public data used by specific plugin, such as storage mode for OpenStack."
        ),
    )
    secret_options = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            "Private data used by specific plugin, such as credentials and hooks."
        ),
    )
    backend_id_rules = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            "Validation rules for resource backend_id: format regex and uniqueness scope."
        ),
    )

    privacy_policy_link = models.URLField(blank=True)
    helpdesk_url = models.URLField(blank=True)
    documentation_url = models.URLField(blank=True)
    country = models.CharField(max_length=2, blank=True)
    type = models.CharField(max_length=100)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    paused_reason = models.TextField(blank=True)
    organization_groups = models.ManyToManyField(
        structure_models.OrganizationGroup, related_name="offerings", blank=True
    )
    tags = models.ManyToManyField(
        "Tag",
        related_name="offerings",
        blank=True,
    )

    # If offering is not shared, it is available only to following user categories:
    # 1) staff user;
    # 2) global support user;
    # 3) users with active permission in original customer;
    # 4) users with active permission in related project.
    shared = models.BooleanField(
        default=True, help_text=_("Accessible to all customers.")
    )

    billable = models.BooleanField(
        default=True, help_text=_("Purchase and usage is invoiced.")
    )
    support_per_user_consumption_limitation = models.BooleanField(
        default=False, help_text=_("Set per user limits for resource components.")
    )
    access_url = models.URLField(
        help_text=_("Publicly accessible offering access URL"), blank=True
    )

    objects = managers.OfferingManager()
    tracker = cast(
        FieldInstanceTracker,
        FieldTracker(
            fields=[
                "compliance_checklist",
                "state",
                "plugin_options",
                "secret_options",
                "name",
            ]
        ),
    )

    class Permissions:
        customer_path = "customer"

    class Meta:
        verbose_name = _("Offering")
        ordering = ["name"]
        indexes = [
            Index(fields=["customer", "state"], name="mp_offering_customer_state_idx"),
            Index(fields=["name"], name="mp_offering_name_idx"),
        ]

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        order_count = quotas_fields.CounterQuotaField(
            target_models=lambda: [Order],
            path_to_scope="offering",
        )
        active_users_count = quotas_fields.QuotaField()
        total_users_count = quotas_fields.QuotaField()
        accepted_consents_count = quotas_fields.QuotaField()
        revoked_consents_count = quotas_fields.QuotaField()
        total_consents_count = quotas_fields.QuotaField()
        revoked_consents_today = quotas_fields.QuotaField()

    @transition(
        field=state,
        source=[States.DRAFT, States.PAUSED, States.UNAVAILABLE],
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

    @transition(field=state, source=States.ACTIVE, target=States.UNAVAILABLE)
    def make_unavailable(self):
        pass

    @transition(field=state, source=States.UNAVAILABLE, target=States.ACTIVE)
    def make_available(self):
        pass

    @transition(field=state, source="*", target=States.ARCHIVED)
    def archive(self):
        pass

    @transition(field=state, source="*", target=States.DRAFT)
    def draft(self):
        pass

    def __str__(self):
        return str(self.name)

    @classmethod
    def get_url_name(cls):
        return "marketplace-provider-offering"

    @cached_property
    def component_factors(self) -> dict[str, int]:
        # get factor from plugin components
        plugin_components = plugins.manager.get_components(self.type)
        return {c.type: c.factor for c in plugin_components}

    @cached_property
    def is_usage_based(self) -> bool:
        """
        Returns True if the offering has at least one component with USAGE billing type.
        Usage-based components are reported periodically and charged based on consumption.
        """
        return self.components.filter(
            billing_type=BillingTypes.USAGE,
        ).exists()

    def get_limit_components(self) -> dict[str, "OfferingComponent"]:
        components = self.components.filter(
            models.Q(billing_type=BillingTypes.LIMIT)
            | models.Q(billing_type=BillingTypes.ONE_TIME, is_prepaid=True)
        )
        return {component.type: component for component in components}

    @cached_property
    def is_limit_based(self) -> bool:
        """
        Returns True if the offering has at least one component with LIMIT billing type
        and the plugin supports updating limits. Limit-based components define a maximum
        quota that can be dynamically adjusted by the user.
        """
        if not plugins.manager.can_update_limits(self.type):
            return False
        if not self.components.filter(billing_type=BillingTypes.LIMIT).exists():
            return False
        return True

    @property
    def is_private(self) -> bool:
        return not self.billable and not self.shared

    def get_datacite_title(self) -> str:
        return self.name

    def get_datacite_creators_name(self) -> str:
        return self.customer.name

    def get_datacite_description(self) -> str:
        return self.description

    def get_datacite_publication_year(self) -> int:
        return self.created.year

    def get_datacite_url(self) -> str:
        return core_utils.format_homeport_link(
            "marketplace-public-offering/{offering_uuid}/", offering_uuid=self.uuid.hex
        )

    def get_users(self, role=None):
        return get_users(self, role)

    @classmethod
    def get_permitted_objects(cls, user):
        return cls.objects.all().filter_for_user(user)

    def update_terms_of_service(
        self, new_terms, new_version=None, requires_reconsent=False
    ):
        """Update terms of service for the offering."""
        tos_config, created = self.terms_of_service_configs.update_or_create(
            is_active=True,
            defaults={
                "terms_of_service": new_terms,
                "version": new_version or "",
                "requires_reconsent": requires_reconsent,
            },
        )

    def has_terms_of_service(self):
        """Check if the offering has terms of service."""
        return self.terms_of_service_configs.filter(is_active=True).exists()

    def get_active_consents(self):
        """Get all active consents for this offering."""
        return self.user_consents.filter(revocation_date__isnull=True)

    def check_user_consent(self, user):
        """Check if a user has active consent for this offering."""
        try:
            consent = self.user_consents.get(user=user, revocation_date__isnull=True)
            return consent
        except UserOfferingConsent.DoesNotExist:
            return None


class UserOfferingConsent(TimeStampedModel, core_models.UuidMixin, LoggableMixin):
    """
    User consent to Terms of Service for specific offerings

    Tracks user consent to offering terms of service. Provides
    comprehensive consent tracking for legal compliance and user transparency.
    """

    user = models.ForeignKey(
        core_models.User, on_delete=models.CASCADE, related_name="offering_consents"
    )
    offering = models.ForeignKey(
        Offering, on_delete=models.CASCADE, related_name="user_consents"
    )
    agreement_date = models.DateTimeField(auto_now_add=True)
    version = models.CharField(max_length=50, blank=True)
    revocation_date = models.DateTimeField(null=True, blank=True)
    tracker = cast(
        FieldInstanceTracker, FieldTracker(fields=["revocation_date", "version"])
    )

    class Meta:
        unique_together = (
            "user",
            "offering",
        )
        verbose_name = _("User offering consent")

    def get_log_fields(self):
        return ("uuid", "version", "agreement_date", "revocation_date")

    def __str__(self):
        return f"{self.user.username} - {self.offering.name}"

    @classmethod
    def get_url_name(cls):
        return "marketplace-user-offering-consent"

    def revoke(self):
        self.revocation_date = timezone.now()
        self.save()

    @property
    def is_revoked(self):
        """Check if the consent has been revoked."""
        return self.revocation_date is not None


class OfferingTermsOfService(TimeStampedModel, core_models.UuidMixin):
    offering = models.ForeignKey(
        Offering, on_delete=models.CASCADE, related_name="terms_of_service_configs"
    )
    terms_of_service = models.TextField(blank=True)
    terms_of_service_link = models.URLField(blank=True)
    version = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=False)
    requires_reconsent = models.BooleanField(
        default=False,
        help_text="If True, user will be asked to re-consent to the terms of service when the terms of service are updated.",
    )
    grace_period_days = models.PositiveIntegerField(
        default=60,
        help_text="Number of days before outdated consents are automatically revoked. Only applies when requires_reconsent=True.",
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["offering"],
                condition=models.Q(is_active=True),
                name="unique_active_terms_per_offering",
            )
        ]

    @property
    def grace_period_end(self):
        if not self.requires_reconsent or not self.is_active:
            return None
        return self.created + timedelta(days=self.grace_period_days)

    def is_grace_period_active(self):
        end_date = self.grace_period_end
        if not end_date:
            return False
        return timezone.now() < end_date

    @property
    def collected_attributes(self):
        """Return list of user attributes that will be collected for this offering."""
        return OfferingUserAttributeConfig.get_exposed_fields_for_offering(
            self.offering
        )


class UserAttributeConfigBase(TimeStampedModel, core_models.UuidMixin):
    """
    Abstract base for models that configure which user attributes are exposed/visible.
    All common personal-data fields live here; subclasses add their FK and unique fields.
    Defaults match the original OfferingUserAttributeConfig defaults for backward
    compatibility (username, full_name, email, registration_method are exposed).
    """

    # Subclasses override to wire up the generic resolver.
    SCOPE_RELATED_NAME: str = ""  # OneToOne reverse accessor name on the scope object
    DEFAULT_CONSTANCE_KEY: str = "DEFAULT_OFFERING_USER_ATTRIBUTES"
    DEFAULT_FALLBACK = ("username", "full_name", "email")
    # Convention: every personal-data toggle on this base is named expose_<attr>.
    EXPOSE_PREFIX = "expose_"

    expose_full_name = models.BooleanField(default=True)
    expose_organization = models.BooleanField(default=False)
    expose_organization_country = models.BooleanField(default=False)
    expose_email = models.BooleanField(default=True)
    expose_affiliations = models.BooleanField(default=False)
    expose_organization_type = models.BooleanField(default=False)
    expose_nationality = models.BooleanField(default=False)
    expose_nationalities = models.BooleanField(default=False)
    expose_country_of_residence = models.BooleanField(default=False)
    expose_eduperson_assurance = models.BooleanField(default=False)
    expose_identity_source = models.BooleanField(default=False)
    expose_username = models.BooleanField(default=True)
    expose_registration_method = models.BooleanField(default=True)
    expose_phone_number = models.BooleanField(default=False)
    expose_job_title = models.BooleanField(default=False)
    expose_gender = models.BooleanField(default=False)
    expose_personal_title = models.BooleanField(default=False)
    expose_place_of_birth = models.BooleanField(default=False)
    expose_address = models.BooleanField(default=False)
    expose_organization_registry_code = models.BooleanField(default=False)
    expose_civil_number = models.BooleanField(default=False)
    expose_birth_date = models.BooleanField(default=False)
    expose_active_isds = models.BooleanField(default=False)

    class Meta:
        abstract = True

    @classmethod
    def _iter_expose_fields(cls):
        """Yield every Django field on the model that follows the expose_<attr> convention."""
        prefix = cls.EXPOSE_PREFIX
        for field in cls._meta.fields:
            if field.name.startswith(prefix):
                yield field

    def get_exposed_fields(self) -> list[str]:
        """Return list of attribute names (without expose_ prefix) that are enabled."""
        prefix = type(self).EXPOSE_PREFIX
        return [
            field.name[len(prefix) :]
            for field in self._iter_expose_fields()
            if getattr(self, field.name)
        ]

    @classmethod
    def get_exposed_fields_for_scope(
        cls, scope_obj, default_attributes=None
    ) -> list[str]:
        """Get exposed fields for a scope object (offering, call, ...).

        Falls back to the subclass-configured Constance key, then to the
        hardcoded DEFAULT_FALLBACK. When iterating many scopes, pre-fetch the
        Constance value once and pass it as default_attributes to avoid
        repeated DB lookups.
        """
        if not cls.SCOPE_RELATED_NAME:
            raise NotImplementedError(f"{cls.__name__} must define SCOPE_RELATED_NAME")
        try:
            config = getattr(scope_obj, cls.SCOPE_RELATED_NAME)
        except cls.DoesNotExist:
            config = None
        if config is not None:
            return config.get_exposed_fields()
        if default_attributes is not None:
            return default_attributes
        return getattr(constance_config, cls.DEFAULT_CONSTANCE_KEY) or list(
            cls.DEFAULT_FALLBACK
        )

    @classmethod
    def get_default_exposed_fields(cls) -> list[str]:
        """Return the Constance-derived list of attribute names exposed by default."""
        return getattr(constance_config, cls.DEFAULT_CONSTANCE_KEY) or list(
            cls.DEFAULT_FALLBACK
        )

    @classmethod
    def get_default_exposure_flags(cls) -> dict[str, bool]:
        """Return {expose_<attr>: bool} for every expose_* field on the model,
        with values derived from the subclass's Constance default. Used to seed
        a freshly-created config row so unspecified booleans don't fall back to
        model-level default=True values silently.
        """
        prefix = cls.EXPOSE_PREFIX
        exposed = set(cls.get_default_exposed_fields())
        return {
            field.name: field.name[len(prefix) :] in exposed
            for field in cls._iter_expose_fields()
        }


class OfferingUserAttributeConfig(UserAttributeConfigBase):
    """
    Configures which user attributes an offering exposes to its service provider.
    Supports GDPR compliance by declaring personal data processing.
    """

    SCOPE_RELATED_NAME = "user_attribute_config"
    DEFAULT_CONSTANCE_KEY = "DEFAULT_OFFERING_USER_ATTRIBUTES"

    offering = models.OneToOneField(
        Offering,
        on_delete=models.CASCADE,
        related_name="user_attribute_config",
    )

    class Meta:
        verbose_name = _("Offering user attribute config")
        verbose_name_plural = _("Offering user attribute configs")

    def __str__(self):
        return f"User attribute config for {self.offering}"

    @classmethod
    def get_url_name(cls):
        return "marketplace-offering-user-attribute-config"

    @classmethod
    def get_exposed_fields_for_offering(
        cls, offering, default_attributes=None
    ) -> list[str]:
        return cls.get_exposed_fields_for_scope(offering, default_attributes)


class OfferingComponent(
    core_models.UuidMixin,
    common_mixins.ProductCodeMixin,
    BaseComponent,
    core_mixins.ScopeMixin,
    core_models.BackendMixin,
):
    """
    Offering-specific component with billing configuration.

    Extends BaseComponent with billing configuration, limits, and validation.
    Supports different billing types (fixed, usage, limit) and provides
    comprehensive component management for offerings.
    """

    components: models.Manager["PlanComponent"]

    class Meta:
        unique_together = ("type", "offering")
        ordering = ("name",)

    tracker = cast(FieldInstanceTracker, FieldTracker())
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name="components"
    )
    parent = models.ForeignKey(
        on_delete=models.CASCADE, to=CategoryComponent, null=True, blank=True
    )
    billing_type = models.CharField(
        choices=BillingTypes.CHOICES, default=BillingTypes.FIXED, max_length=5
    )
    # limit_period and limit_amount fields are used if billing_type is USAGE or LIMIT
    limit_period = models.CharField(
        choices=LimitPeriods.CHOICES, default=LimitPeriods.MONTH, max_length=10
    )
    limit_amount = models.IntegerField(blank=True, null=True)
    # unit_factor is for metadata only and is not involved in any computations in Mastermind
    unit_factor = models.IntegerField(
        default=1,
        help_text=_("The conversion factor from backend units to measured_unit"),
    )
    # max_value and min_value fields are used if billing_type is LIMIT
    max_value = models.IntegerField(blank=True, null=True)
    min_value = models.IntegerField(blank=True, null=True)
    max_available_limit = models.IntegerField(blank=True, null=True)
    # is_boolean field allows to render checkbox in UI which set limit amount to 1
    is_boolean = models.BooleanField(default=False)
    # default_limit field is used by UI to prefill limit values
    default_limit = models.IntegerField(blank=True, null=True)
    # following fields are used for prepaid billing
    is_prepaid = models.BooleanField(default=False)
    overage_component = models.ForeignKey(
        on_delete=models.CASCADE, to="OfferingComponent", null=True, blank=True
    )
    min_prepaid_duration = models.IntegerField(blank=True, null=True)
    max_prepaid_duration = models.IntegerField(blank=True, null=True)
    prepaid_duration_step = models.PositiveIntegerField(
        blank=True,
        null=True,
        default=None,
        help_text=_(
            "Step size in months for the initial prepaid duration at order creation. "
            "If set, only multiples of this value (starting from min_prepaid_duration) "
            "are valid. Defaults to 1 (any value between min and max)."
        ),
    )
    min_renewal_duration = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=_("Minimum number of months allowed for a renewal."),
    )
    max_renewal_duration = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=_("Maximum number of months allowed for a renewal."),
    )
    renewal_duration_step = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=_(
            "Step size in months for renewal. Only multiples of this value "
            "(starting from min_renewal_duration) are valid. Defaults to 1."
        ),
    )
    objects = managers.MixinManager("scope")

    def save(self, *args, **kwargs):
        if not self.limit_period:
            self.limit_period = LimitPeriods.MONTH
        super().save(*args, **kwargs)

    def validate_amount(self, resource, amount, date):
        if not self.limit_period or not self.limit_amount:
            return

        usages = ComponentUsage.objects.filter(resource=resource, component=self)

        if self.limit_period == LimitPeriods.MONTH:
            usages = usages.filter(billing_period=core_utils.month_start(date))
        elif self.limit_period == LimitPeriods.QUARTERLY:
            # Convert date to datetime for quarter calculations
            if isinstance(date, timezone.datetime):
                datetime_obj = date
            else:
                datetime_obj = timezone.datetime.combine(
                    date, timezone.datetime.min.time()
                )
                datetime_obj = timezone.make_aware(datetime_obj)

            quarter_start = core_utils.get_quarter_start(datetime_obj)
            quarter_end = core_utils.get_quarter_end(datetime_obj)
            usages = usages.filter(
                billing_period__gte=quarter_start.date(),
                billing_period__lte=quarter_end.date(),
            )
        elif self.limit_period == LimitPeriods.ANNUAL:
            usages = usages.filter(billing_period__year=date.year)

        total = usages.aggregate(models.Sum("usage"))["usage__sum"] or 0

        if total + amount > self.limit_amount:
            raise rf_exceptions.ValidationError(
                _("Total amount exceeds limit. Total amount: %s, limit: %s.")
                % (total + amount, self.limit_amount)
            )

    @property
    def is_builtin(self) -> bool:
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
    Pricing plan model with component-based pricing.

    Defines pricing plans for offerings with component-based pricing,
    organization group restrictions, and cost estimation capabilities.
    Supports different billing types and pricing models.
    """

    """
    Plan unit price is computed as a sum of its fixed components'
    price multiplied by component amount when offering with plans
    is created via REST API. Usage-based components don't contribute to plan price.
    It is assumed that plan price is updated manually when plan component is managed via Django ORM.
    """

    id: int
    components: models.Manager["PlanComponent"]

    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name="plans"
    )
    archived = models.BooleanField(
        default=False, help_text=_("Forbids creation of new resources.")
    )
    objects = managers.MixinManager("scope")
    max_amount = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1)],
        help_text=_(
            "Maximum number of plans that could be active. "
            "Plan is disabled when maximum amount is reached."
        ),
    )
    organization_groups = models.ManyToManyField(
        structure_models.OrganizationGroup, related_name="plans", blank=True
    )
    objects = managers.PlanManager()
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta:
        ordering = ("name",)

    @classmethod
    def get_url_name(cls):
        return "marketplace-plan"

    class Permissions:
        customer_path = "offering__customer"

    def get_estimate(self, limits=None, start_date=None, end_date=None):
        """Estimate total cost for given limits.

        For prepaid components, multiplies by the number of months between
        start_date and end_date. Without dates, returns single-period cost.
        """

        cost = self.unit_price

        if limits:
            components_map = self.offering.get_limit_components()
            component_prices = {
                c.component.type: c.price for c in self.components.all()
            }

            factors = self.offering.component_factors

            for key, component in components_map.items():
                price = component_prices.get(key, 0)
                limit = limits.get(key, 0)
                factor = factors.get(key, 1)
                per_unit = Decimal(price) * Decimal(str(limit)) / Decimal(str(factor))

                if component.is_prepaid and start_date and end_date:
                    months = core_utils.calculate_duration_months(start_date, end_date)
                    per_unit *= months

                cost += per_unit

        return cost

    def __str__(self):
        return str(self.name)

    @property
    def fixed_price(self) -> float:
        return self.sum_components(BillingTypes.FIXED)

    @property
    def init_price(self) -> float:
        return self.sum_components(BillingTypes.ONE_TIME)

    @property
    def non_prepaid_init_price(self) -> float:
        """Activation fee excluding prepaid ONE_TIME components.

        Prepaid ONE_TIME components are already accounted for in
        get_estimate() with the duration multiplier. This avoids
        double-counting them via init_price.
        """
        components = self.components.filter(
            component__billing_type=BillingTypes.ONE_TIME,
            component__is_prepaid=False,
        )
        return (
            components.aggregate(
                sum=models.Sum(models.F("price") * models.F("amount"))
            )["sum"]
            or 0
        )

    @property
    def switch_price(self) -> float:
        return self.sum_components(BillingTypes.ON_PLAN_SWITCH)

    def sum_components(self, billing_type) -> float:
        components = self.components.filter(component__billing_type=billing_type)
        return (
            components.aggregate(
                sum=models.Sum(models.F("price") * models.F("amount"))
            )["sum"]
            or 0
        )

    @property
    def has_connected_resources(self) -> bool:
        return Resource.objects.filter(plan=self).exists()

    @property
    def is_active(self) -> bool:
        if not self.max_amount:
            return True
        usage = (
            Resource.objects.filter(plan=self)
            .exclude(state=ResourceStates.TERMINATED)
            .count()
        )
        return self.max_amount > usage

    def get_log_fields(self):
        return (
            "name",
            "uuid",
            "offering",
        )


class PlanComponent(LoggableMixin, models.Model):
    """
    Component pricing within plans.

    Defines pricing for individual components within plans with
    amount, price, and future price tracking. Used for detailed
    cost calculation and billing management.
    """

    class Meta:
        unique_together = ("plan", "component")
        ordering = ("component__name",)

    plan = models.ForeignKey(
        on_delete=models.CASCADE, to=Plan, related_name="components"
    )
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
        related_name="components",
        null=True,
    )
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(
        default=0,
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("Price per unit per billing period."),
    )
    future_price = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("Price per unit for future month."),
        null=True,
    )
    discount_threshold = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("Minimum amount to be eligible for discount."),
    )
    discount_rate = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("Discount rate in percentage."),
    )
    tracker = cast(FieldInstanceTracker, FieldTracker())

    @property
    def has_connected_resources(self):
        return self.plan.has_connected_resources

    def __str__(self):
        name = self.component and self.component.name
        if name:
            return f"{name}, plan: {self.plan.name}"
        return "for plan: %s" % self.plan.name

    def get_log_fields(self):
        return (
            "plan",
            "component",
            "amount",
            "price",
            "future_price",
        )


class Screenshot(
    core_models.UuidMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
    core_models.NameMixin,
    core_models.BackendMixin,
):
    """
    Visual content for offerings.

    Provides screenshot functionality for offerings with image
    and thumbnail support. Used for visual presentation of
    offerings in the marketplace.
    """

    image = models.ImageField(upload_to=get_upload_path)
    thumbnail = models.ImageField(upload_to=get_upload_path, editable=False, null=True)
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name="screenshots"
    )

    class Permissions:
        customer_path = "offering__customer"

    class Meta:
        verbose_name = _("Screenshot")

    def __str__(self):
        return str(self.name)

    @classmethod
    def get_url_name(cls):
        return "marketplace-screenshot"


class CostEstimateMixin(models.Model):
    """
    Mixin for cost estimation functionality.

    Provides cost estimation with plan-based calculations and policy
    validation. Used for calculating costs based on limits and plans
    with policy compliance checking.
    """

    class Meta:
        abstract = True

    # Cost estimate is computed with respect to limit components
    cost = models.DecimalField(max_digits=22, decimal_places=10, null=True, blank=True)
    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan, null=True, blank=True)
    limits = models.JSONField(blank=True, default=dict)

    def _get_cost_dates(self):
        """Return (start_date, end_date) for prepaid duration calculation."""
        start_date = getattr(self, "start_date", None) or timezone.now().date()
        end_date = getattr(self, "end_date", None)
        return start_date, end_date

    def init_cost(self):
        if not self.plan:
            return
        try:
            start_date, end_date = self._get_cost_dates()
            self.cost = self.plan.get_estimate(self.limits, start_date, end_date)

            # Proactive policy validation for new resources
            if self._should_validate_cost_policies():
                self._validate_project_cost_policies()

        except PolicyException:
            self.set_state_erred()
            self.error_message = "Policy is violated."

    def _should_validate_cost_policies(self):
        """
        Determine if we should proactively validate cost policies.

        Returns True only for NEW Resource instances (not Orders, not updates).
        """

        # Only validate for Resource instances, not Order instances
        if not isinstance(self, Resource):
            return False

        # Only validate for new resources (pk is None before first save)
        if self.pk is not None:
            return False

        # Must have a calculated cost to validate
        if self.cost is None:
            return False

        return True

    def _validate_project_cost_policies(self):
        """
        Emit signal to allow external modules to validate resource creation.

        This prevents the first resource from bypassing cost limits when policy.has_fired=False.
        External modules (e.g., policy) can subscribe to this signal and raise PolicyException.
        """
        # Emit signal with resource and cost information
        # External validators (policy module) can raise PolicyException
        signals.resource_creation_validation.send(
            sender=self.__class__, resource=self, cost=self.cost
        )


class SafeAttributesMixin(models.Model):
    """
    Mixin for safe attribute handling.

    Provides safe attribute functionality excluding secret attributes.
    Used for secure attribute access that filters out sensitive
    information like passwords and credentials.
    """

    class Meta:
        abstract = True

    offering = models.ForeignKey(Offering, related_name="+", on_delete=models.CASCADE)
    attributes = models.JSONField(blank=True, default=dict)

    @property
    def safe_attributes(self) -> dict:
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


class ResourceDetailsMixin(
    SafeAttributesMixin,
    CostEstimateMixin,
    core_models.NameMixin,
    core_models.SlugMixin,
    core_models.DescribableMixin,
):
    """
    Mixin combining resource details with cost estimation.

    Provides comprehensive resource details including cost estimation,
    safe attributes, and end date management. Used for resource
    lifecycle management and billing calculations.
    """

    class Meta:
        abstract = True

    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_(
            "The date is inclusive. Once reached, a resource will be scheduled for termination."
        ),
    )
    end_date_requested_by = models.ForeignKey[core_models.User](
        on_delete=models.SET_NULL,
        to=core_models.User,
        blank=True,
        null=True,
        related_name="+",
    )


class Resource(
    ResourceDetailsMixin,
    core_models.UuidMixin,
    core_models.BackendMixin,
    TimeStampedModel,
    core_mixins.ScopeMixin,
    structure_models.StructureLoggableMixin,
    common_mixins.BackendMetadataMixin,
    core_models.ErrorMessageMixin,
    core_models.LastSyncMixin,
):
    """
    Core marketplace resource model representing provisioned services.

    Represents provisioned services with state management, usage tracking,
    and backend integration. Provides comprehensive resource lifecycle
    management including billing, quotas, and access control.

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

    id: int
    offering_id: int
    children: models.Manager["Resource"]
    quotas: models.Manager["ComponentQuota"]
    usages: models.Manager["ComponentUsage"]
    endpoints: models.Manager["ResourceAccessEndpoint"]
    users: models.Manager["ResourceUser"]
    get_state_display: Callable[[], str]

    class States(ResourceStates):
        pass

    class Permissions:
        customer_path = "project__customer"
        project_path = "project"
        list_permission = PermissionEnum.LIST_RESOURCES

    class Meta:
        ordering = ["created"]
        unique_together = ("content_type", "object_id")
        indexes = [
            Index(fields=["offering", "state"], name="mp_resource_offering_state_idx"),
            Index(fields=["project", "state"], name="mp_resource_project_state_idx"),
        ]

    state = FSMIntegerField(default=States.CREATING, choices=States.CHOICES)
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    parent = models.ForeignKey["Resource"](
        on_delete=models.SET_NULL,
        to="Resource",
        related_name="children",
        null=True,
        blank=True,
    )
    report = models.JSONField(blank=True, null=True)
    options = models.JSONField(blank=True, null=True)
    current_usages = models.JSONField(
        blank=True,
        default=dict,
        help_text="Dictionary mapping component types to their current usage amounts. "
        "For usage-based and limit-based components, it stores the most recent reported amounts or consumed quotas. "
        "Populated by backend synchronization tasks or explicit usage reports.",
    )
    tracker = cast(FieldInstanceTracker, FieldTracker())
    objects = managers.ResourceManager()
    # Effective ID is used when resource is provisioned through remote Waldur
    effective_id = models.CharField(max_length=255, blank=True)
    downscaled = models.BooleanField(default=False)
    restrict_member_access = models.BooleanField(default=False)
    paused = models.BooleanField(default=False)

    NON_LOGGABLE_FIELDS = (
        "modified",
        "backend_metadata",
        "action",
        "report",
        "action_details",
        "plan",
        "object_id",
        "content_type_id",
        "error_message",
        "error_traceback",
        "current_usages",
    )

    @property
    def customer(self) -> structure_models.Customer:
        return self.project.customer

    @transition(
        field=state,
        source=[States.ERRED, States.CREATING, States.UPDATING, States.TERMINATING],
        target=States.OK,
    )
    def set_state_ok(self):
        pass

    @transition(
        field=state,
        source=[States.TERMINATED, States.ERRED],
        target=States.CREATING,
    )
    def set_state_creating(self):
        pass

    @transition(field=state, source="*", target=States.ERRED)
    def set_state_erred(self):
        pass

    @transition(field=state, source="*", target=States.UPDATING)
    def set_state_updating(self):
        pass

    @transition(field=state, source="*", target=States.TERMINATING)
    def set_state_terminating(self):
        pass

    @transition(field=state, source="*", target=States.TERMINATED)
    def set_state_terminated(self):
        pass

    @property
    def backend_uuid(self) -> str | None:
        if self.scope:
            return self.scope.uuid

    @property
    def backend_type(self) -> str | None:
        if self.scope:
            scope_type = self.scope.get_scope_type()
            return scope_type if scope_type else "Marketplace.Resource"

    def get_log_fields(self):
        return (
            "uuid",
            "name",
            "project",
            "offering",
            "created",
            "modified",
            "attributes",
            "cost",
            "plan",
            "limits",
            "get_state_display",
            "backend_metadata",
            "backend_uuid",
            "backend_type",
            "backend_id",
            "effective_id",
            "slug",
        )

    @classmethod
    def get_scope_type(cls):
        return "Marketplace.Resource"

    @classmethod
    def get_url_name(cls):
        return "marketplace-resource"

    def get_homeport_link(self) -> str:
        return core_utils.format_homeport_link(
            "resource-details/{resource_uuid}/",
            resource_uuid=self.uuid.hex,
        )

    @property
    def is_expired(self) -> bool:
        if not self.end_date:
            return False
        return self.end_date <= timezone.datetime.today().date()

    def __str__(self):
        if self.name:
            return f"{self.name} ({self.offering.name})"
        if self.scope:
            return f"{self.name} ({self.content_type} / {self.object_id})"

        return f"{self.uuid} ({self.offering.name})"

    @property
    def creation_order(self) -> "Order | None":
        return self.order_set.filter(type=OrderTypes.CREATE).first()

    @property
    def order_in_progress(self) -> "Order | None":
        return self.order_set.filter(state__in=OrderStates.PENDING_STATES).first()

    def get_prepaid_balance(
        self, offering_component: "OfferingComponent", excluded_ids: list | None = None
    ) -> int:
        """
        Calculates the comprehensive remaining prepaid balance for a component.

        This method uses a consistent SUBSCRIPTION-BASED logic for all periodic limits,
        meaning periods are calculated relative to the resource's creation date.

        The behavior for each period is as follows:
        - MONTH: Usage is summed for the current subscription month (e.g., from the
                 15th of one month to the 14th of the next).
        - QUARTERLY: Usage is summed for the current 3-month subscription quarter.
        - ANNUAL: Usage is summed for the current 12-month subscription year.
        - TOTAL: Usage is summed over the entire lifetime of the resource.

        Args:
            offering_component (OfferingComponent): The component object to check.
            excluded_ids (list, optional): A list of ComponentUsage primary keys
                                            to exclude from the sum. This is crucial
                                            for getting the balance *before* an update.

        Returns:
            int: The remaining non-negative prepaid quota. Overage is represented
                 as a balance of 0.
        """
        # 1. Get the initial quota purchased by the customer for the period.
        initial_quota = self.limits.get(offering_component.type, 0)
        if not isinstance(initial_quota, int | float) or initial_quota <= 0:
            return 0

        # 2. Determine the time window for summing up usage based on the limit period.
        now = timezone.now()
        period_start, period_end = None, None
        limit_period = offering_component.limit_period

        # Calculate the time delta from creation to now for all periodic calculations.
        delta = relativedelta(now, self.created)
        total_months_passed = delta.years * 12 + delta.months

        if limit_period == LimitPeriods.MONTH:
            # SUBSCRIPTION-BASED MONTH: The period is relative to the creation day.
            period_start = self.created + relativedelta(months=total_months_passed)
            period_end = (
                period_start + relativedelta(months=1) - relativedelta(microseconds=1)
            )

        elif limit_period == LimitPeriods.QUARTERLY:
            # SUBSCRIPTION-BASED QUARTER: A 3-month period relative to creation.
            num_quarters_passed = total_months_passed // 3
            months_to_add = num_quarters_passed * 3
            period_start = self.created + relativedelta(months=months_to_add)
            period_end = (
                period_start + relativedelta(months=3) - relativedelta(microseconds=1)
            )

        elif limit_period == LimitPeriods.ANNUAL:
            # SUBSCRIPTION-BASED YEAR: A 12-month period relative to creation.
            num_years_passed = total_months_passed // 12
            period_start = self.created + relativedelta(years=num_years_passed)
            period_end = (
                period_start + relativedelta(years=1) - relativedelta(microseconds=1)
            )

        elif limit_period == LimitPeriods.TOTAL:
            # SUBSCRIPTION-BASED LIFETIME: The entire resource duration.
            period_start = self.created
            period_end = self.end_date or now

        else:
            # If limit_period is not set or invalid, there is no balance.
            return 0

        # 3. Sum the total usage reported for this component within the calculated time window.
        usage_query = self.usages.filter(
            component=offering_component,
            date__gte=period_start,
            date__lte=period_end,
        )

        if excluded_ids:
            usage_query = usage_query.exclude(pk__in=excluded_ids)

        usage_aggregation = usage_query.aggregate(total_usage=Sum("usage"))
        total_usage = usage_aggregation.get("total_usage") or 0

        # 4. Calculate the final balance and ensure it's not negative.
        balance = initial_quota - total_usage

        return max(0, int(balance))

    def get_renewal_cost(
        self, extension_months: int, new_limits: dict | None = None
    ) -> Decimal:
        """
        Calculates the cost for renewing a prepaid resource.

        Uses the same pricing logic as Plan.get_estimate: for each prepaid
        component, cost = price × limit / factor × months.

        Args:
            extension_months: Number of months to extend the subscription.
            new_limits: New limits for the renewal period. Defaults to
                current resource limits.

        Returns:
            Decimal: The total calculated cost for the renewal.
        """
        if not self.plan:
            return Decimal("0.0")

        final_limits = new_limits or self.limits

        # Compute prepaid cost for the extension period.
        # Use a synthetic date range so calculate_duration_months returns
        # exactly extension_months.
        start_date = timezone.now().date()
        end_date = start_date + relativedelta(months=extension_months)

        total = Decimal("0.0")
        components_map = self.offering.get_limit_components()
        component_prices = {
            c.component.type: c.price for c in self.plan.components.all()
        }
        factors = self.offering.component_factors

        for key, component in components_map.items():
            if not component.is_prepaid:
                continue
            price = component_prices.get(key, 0)
            limit = final_limits.get(key, 0)
            factor = factors.get(key, 1)

            months = core_utils.calculate_duration_months(start_date, end_date)
            total += (
                Decimal(str(price))
                * Decimal(str(limit))
                / Decimal(str(factor))
                * months
            )

        return total

    def get_renewal_estimate(
        self, extension_months: int, new_limits: dict | None = None
    ) -> dict:
        """
        Returns a detailed cost breakdown for renewal including prepaid
        subscription components and limit-based component changes.
        """
        components = []
        subscription_total = Decimal("0.0")
        limit_change_total = Decimal("0.0")
        final_limits = new_limits or self.limits

        # Calculate new end date
        current_end_date = self.end_date or timezone.now().date()
        if current_end_date < timezone.now().date():
            current_end_date = timezone.now().date()
        new_end_date = current_end_date + relativedelta(months=extension_months)
        remaining_days = (new_end_date - timezone.now().date()).days

        if self.plan:
            component_factors = self.offering.component_factors

            # 1. Subscription items (prepaid components)
            for plan_component in self.plan.components.filter(
                component__is_prepaid=True
            ):
                component = plan_component.component
                if not component:
                    continue
                limit_amount = Decimal(str(final_limits.get(component.type, 0)))
                factor = Decimal(str(component_factors.get(component.type, 1)))
                display_amount = limit_amount / factor
                current_limit = Decimal(str(self.limits.get(component.type, 0)))
                current_display = current_limit / factor
                total = plan_component.price * display_amount * extension_months
                components.append(
                    {
                        "component_type": component.type,
                        "component_name": component.name,
                        "billing_type": "prepaid",
                        "billing_period": None,
                        "current_limit": current_display,
                        "new_limit": display_amount,
                        "unit_price": plan_component.price,
                        "measured_unit": component.measured_unit or "",
                        "period_description": f"{extension_months} months",
                        "total": total,
                    }
                )
                subscription_total += total

            # 2. Limit change items (billing_type='limit', where new_limit > current_limit)
            for plan_component in self.plan.components.filter(
                component__billing_type=BillingTypes.LIMIT,
                component__is_prepaid=False,
            ):
                component = plan_component.component
                if not component:
                    continue

                current_limit = self.limits.get(component.type, 0)
                new_limit = final_limits.get(component.type, 0)
                delta = new_limit - current_limit

                if delta <= 0:
                    continue

                # Skip components with no periodic billing
                limit_period = component.limit_period
                if not limit_period or limit_period == LimitPeriods.TOTAL:
                    continue

                billing_period_days = {
                    LimitPeriods.MONTH: 30,
                    LimitPeriods.QUARTERLY: 91,
                    LimitPeriods.ANNUAL: 365,
                }.get(limit_period)

                if not billing_period_days:
                    continue

                total = (
                    plan_component.price * delta * remaining_days / billing_period_days
                )

                period_label = {
                    LimitPeriods.MONTH: "mo",
                    LimitPeriods.QUARTERLY: "qtr",
                    LimitPeriods.ANNUAL: "yr",
                }.get(limit_period, limit_period)

                components.append(
                    {
                        "component_type": component.type,
                        "component_name": component.name,
                        "billing_type": "limit",
                        "billing_period": limit_period,
                        "current_limit": current_limit,
                        "new_limit": new_limit,
                        "unit_price": plan_component.price,
                        "measured_unit": component.measured_unit or "",
                        "period_description": f"{remaining_days}d / {billing_period_days}d ({period_label})",
                        "total": total,
                    }
                )
                limit_change_total += total

        return {
            "components": components,
            "subscription_total": subscription_total,
            "limit_change_total": limit_change_total,
            "total": subscription_total + limit_change_total,
            "remaining_days": remaining_days,
            "new_end_date": new_end_date,
        }


class ResourcePlanPeriod(TimeStampedModel, TimeFramedModel, core_models.UuidMixin):
    """
    Time-framed billing plan tracking for resources.

    Tracks billing plans for specific timeframes during resource lifecycle.
    Allows for plan changes over time and maintains historical billing
    information for accurate cost calculation.
    """

    """
    This model allows to track billing plan for timeframes during resource lifecycle.
    """

    components: models.Manager["ComponentUsage"]

    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name="+"
    )
    plan = models.ForeignKey(on_delete=models.CASCADE, to=Plan)

    def __str__(self):
        return str(self.resource.name)

    @property
    def current_components(self):
        now = timezone.now()
        return self.components.filter(billing_period=core_utils.month_start(now))


class Order(
    core_models.UuidMixin,
    core_models.BackendMixin,
    core_models.ErrorMessageMixin,
    core_models.SlugMixin,
    CostEstimateMixin,
    structure_models.StructureLoggableMixin,
    SafeAttributesMixin,
    TimeStampedModel,
):
    """
    Order processing model with approval workflow.

    Manages order lifecycle with state transitions, approval workflow,
    and review tracking by consumers and providers. Supports different
    order types and comprehensive order management.
    """

    type = models.PositiveSmallIntegerField(
        choices=OrderTypes.CHOICES, default=OrderTypes.CREATE
    )

    old_plan = models.ForeignKey(
        on_delete=models.CASCADE, to=Plan, related_name="+", null=True, blank=True
    )
    project = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.Project)
    resource = models.ForeignKey(on_delete=models.CASCADE, to=Resource)
    state = FSMIntegerField(
        default=OrderStates.PENDING_CONSUMER, choices=OrderStates.CHOICES
    )
    output = models.TextField(blank=True)
    output_updated_at = models.DateTimeField(editable=False, null=True, blank=True)
    error_updated_at = models.DateTimeField(editable=False, null=True, blank=True)
    tracker = cast(FieldInstanceTracker, FieldTracker())

    created_by = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=core_models.User,
        related_name="+",
        null=True,
    )
    consumer_reviewed_by = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=core_models.User,
        blank=True,
        null=True,
        related_name="+",
    )
    consumer_reviewed_at = models.DateTimeField(editable=False, null=True, blank=True)
    provider_reviewed_by = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=core_models.User,
        blank=True,
        null=True,
        related_name="+",
    )
    provider_reviewed_at = models.DateTimeField(editable=False, null=True, blank=True)
    callback_url = models.URLField(null=True, blank=True)
    termination_comment = models.CharField(blank=True, null=True, max_length=255)
    completed_at = models.DateTimeField(
        _("completion time"), null=True, blank=True, editable=False
    )
    start_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Enables delayed processing of resource provisioning order."),
    )
    request_comment = models.CharField(blank=True, null=True, max_length=255)
    attachment = models.FileField(
        upload_to="marketplace_order_attachments",
        blank=True,
        null=True,
        validators=[FileTypeValidator(allowed_types=["application/pdf"])],
    )

    # Provider-to-consumer communication
    provider_message = models.TextField(blank=True, default="")
    provider_message_url = models.URLField(blank=True, default="")
    provider_message_attachment = models.FileField(
        upload_to="marketplace_order_provider_attachments",
        blank=True,
        null=True,
        validators=[FileTypeValidator(allowed_types=["application/pdf"])],
    )

    # Consumer-to-provider response
    consumer_message = models.TextField(blank=True, default="")
    consumer_message_attachment = models.FileField(
        upload_to="marketplace_order_consumer_attachments",
        blank=True,
        null=True,
        validators=[FileTypeValidator(allowed_types=["application/pdf"])],
    )

    consumer_rejection_comment = models.TextField(blank=True, default="")
    provider_rejection_comment = models.TextField(blank=True, default="")

    get_type_display: Callable[[], str]

    class Permissions:
        customer_path = "project__customer"
        project_path = "project"
        list_permission = PermissionEnum.LIST_ORDERS

    class Meta:
        verbose_name = _("Order")
        ordering = ("created",)
        indexes = [
            Index(fields=["state", "-created"], name="mp_order_state_created_idx"),
        ]

    def _get_cost_dates(self):
        """Return (start_date, end_date) for prepaid duration calculation.

        For orders, end_date comes from the resource or order attributes.
        """
        start_date = self.start_date or timezone.now().date()
        end_date = None
        if self.resource_id and self.resource and self.resource.end_date:
            end_date = self.resource.end_date
        elif self.attributes.get("end_date"):
            try:
                end_date = datetime.date.fromisoformat(self.attributes["end_date"])
            except (ValueError, TypeError):
                pass
        return start_date, end_date

    def init_cost(self):
        super().init_cost()
        if self.plan:
            if self.type == OrderTypes.CREATE:
                self.cost += self.plan.non_prepaid_init_price
            elif self.type == OrderTypes.UPDATE:
                self.cost += self.plan.switch_price

    @property
    def fixed_price(self) -> float:
        if self.type == OrderTypes.CREATE:
            return self.plan.fixed_price
        return 0

    @property
    def old_cost_estimate(self) -> float:
        if "old_limits" not in self.attributes:
            return 0
        plan = self.old_plan or self.plan
        if not plan:
            return 0

        # For renewals, include the old subscription duration
        start_date = None
        end_date = None
        old_end_date_str = self.attributes.get("old_end_date")
        if old_end_date_str:
            try:
                end_date = datetime.date.fromisoformat(old_end_date_str)
            except (ValueError, TypeError):
                pass
        if end_date and self.resource_id and self.resource:
            start_date = (
                self.resource.created.date()
                if hasattr(self.resource.created, "date")
                else self.resource.created
            )

        return plan.get_estimate(self.attributes["old_limits"], start_date, end_date)

    @property
    def activation_price(self) -> float:
        if self.type == OrderTypes.CREATE:
            return self.plan.non_prepaid_init_price
        elif self.type == OrderTypes.UPDATE:
            return self.plan.switch_price
        return 0

    @classmethod
    def get_url_name(cls):
        return "marketplace-order"

    def generate_slug(self) -> str:
        return generate_slug(
            f"{self.project.customer.slug}-{self.offering.slug}-{timezone.now().date().isoformat()}",
            self.__class__,
        )

    def review_by_consumer(self, user=None):
        self.consumer_reviewed_by = user
        self.consumer_reviewed_at = timezone.now()
        self.save()

    def review_by_provider(self, user=None):
        self.provider_reviewed_by = user
        self.provider_reviewed_at = timezone.now()
        self.save()

    def _set_completed_at_timestamp(self):
        logger.debug("Setting order %s completion time", self)
        self.completed_at = timezone.now()
        self.save()

    @transition(field=state, source=OrderStates.EXECUTING, target=OrderStates.DONE)
    def complete(self):
        self._set_completed_at_timestamp()

    @transition(field=state, source="*", target=OrderStates.CANCELED)
    def cancel(self, termination_comment=None):
        if termination_comment:
            self.termination_comment = termination_comment
        self._set_completed_at_timestamp()

    @transition(
        field=state,
        source=(OrderStates.PENDING_CONSUMER, OrderStates.PENDING_PROVIDER),
        target=OrderStates.REJECTED,
    )
    def reject(self):
        self._set_completed_at_timestamp()

    @transition(field=state, source="*", target=OrderStates.ERRED)
    def fail(self):
        self._set_completed_at_timestamp()

    @transition(
        field=state,
        source=[
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.PENDING_PROJECT,
            OrderStates.PENDING_START_DATE,
            OrderStates.ERRED,
        ],
        target=OrderStates.EXECUTING,
    )
    def set_state_executing(self):
        pass

    @transition(field=state, source="*", target=OrderStates.ERRED)
    def set_state_erred(self):
        pass

    def save(self, *args, **kwargs):
        # Track the latest updates for output and error details explicitly.
        update_fields = kwargs.get("update_fields")
        normalized_update_fields = (
            set(update_fields) if update_fields is not None else None
        )

        output_changed = self.pk and self.tracker.has_changed("output")
        error_message_changed = self.pk and self.tracker.has_changed("error_message")
        error_traceback_changed = self.pk and self.tracker.has_changed(
            "error_traceback"
        )
        if output_changed or error_message_changed or error_traceback_changed:
            now = timezone.now()
            if output_changed:
                self.output_updated_at = now
            if error_message_changed or error_traceback_changed:
                self.error_updated_at = now

            if normalized_update_fields is not None:
                if output_changed:
                    normalized_update_fields.add("output")
                    normalized_update_fields.add("output_updated_at")
                if error_message_changed:
                    normalized_update_fields.add("error_message")
                if error_traceback_changed:
                    normalized_update_fields.add("error_traceback")
                if error_message_changed or error_traceback_changed:
                    normalized_update_fields.add("error_updated_at")

        if normalized_update_fields is not None:
            kwargs["update_fields"] = list(normalized_update_fields)

        super().save(*args, **kwargs)

    def get_log_fields(self):
        return (
            "uuid",
            "created",
            "modified",
            "cost",
            "limits",
            "attributes",
            "offering",
            "resource",
            "project",
            "plan",
            "get_state_display",
            "get_type_display",
            "created_by",
            "consumer_reviewed_by",
            "consumer_reviewed_at",
            "provider_reviewed_by",
            "provider_reviewed_at",
            "output_updated_at",
            "error_updated_at",
        )

    def __str__(self):
        return f"UUID: {self.uuid}, type: {self.get_type_display()}, offering: {self.offering}, created_by: {self.created_by}"


class ComponentQuota(TimeStampedModel):
    """
    Resource-level quota management for components.

    Manages quotas for resource components with limit and usage tracking.
    Provides quota enforcement and monitoring for resource consumption.
    """

    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name="quotas"
    )
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
    )
    limit = models.DecimalField(default=-1, decimal_places=2, max_digits=20)
    usage = models.DecimalField(default=0, decimal_places=2, max_digits=20)

    class Meta:
        unique_together = ("resource", "component")

    def __str__(self):
        return f"resource: {self.resource.name}, component: {self.component.name}"


class ComponentUsage(
    TimeStampedModel,
    core_models.DescribableMixin,
    core_models.BackendMixin,
    core_models.UuidMixin,
    LoggableMixin,
):
    """
    Detailed usage tracking for resource components.

    Tracks component usage with billing period and plan period associations.
    Supports recurring usage patterns and provides detailed consumption
    data for billing and reporting.
    """

    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name="usages"
    )
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
    )
    usage = models.DecimalField(default=0, decimal_places=2, max_digits=20)
    date = models.DateTimeField()
    plan_period = models.ForeignKey["ResourcePlanPeriod"](
        on_delete=models.CASCADE,
        to="ResourcePlanPeriod",
        related_name="components",
        null=True,
    )
    billing_period = models.DateField()
    recurring = models.BooleanField(
        default=False, help_text="Reported value is reused every month until changed."
    )
    modified_by = models.ForeignKey[User](
        to=User, related_name="+", blank=True, null=True, on_delete=models.SET_NULL
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["resource", "component", "plan_period", "billing_period"],
                name="unique_with_optional",
            ),
            UniqueConstraint(
                fields=["resource", "component", "billing_period"],
                condition=models.Q(plan_period=None),
                name="unique_without_optional",
            ),
        ]
        indexes = [
            Index(fields=["resource", "-date"], name="mp_compusage_resource_date_idx"),
        ]

    def __str__(self):
        return f"resource: {self.resource.name}, component: {self.component.name}"

    def get_log_fields(self):
        return ("uuid", "description", "usage", "date", "resource", "component")


class ComponentUsagePollRecord(models.Model):
    """Tracks the latest usage poll state per resource+component for staff debugging.

    One row per (resource, component) — upserted on each poll.
    Replaces Django cache for last_poll_time tracking and provides
    staff observability into usage-based billing accumulation.
    """

    resource = models.ForeignKey(
        Resource, on_delete=models.CASCADE, related_name="usage_poll_records"
    )
    component = models.ForeignKey(OfferingComponent, on_delete=models.CASCADE)
    last_poll_time = models.DateTimeField(
        help_text="When this resource+component was last polled."
    )
    raw_usage = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        help_text="Raw value from the backend API at last poll.",
    )
    elapsed_hours = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Hours since previous poll (capped at 24h).",
    )
    increment = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        help_text="raw_usage × elapsed_hours added to the running total.",
    )
    accumulated_total = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        help_text="Running total for the current billing period.",
    )
    billing_period = models.DateField(help_text="Month this accumulation belongs to.")

    class Meta:
        unique_together = ("resource", "component")

    def __str__(self):
        return (
            f"{self.resource.name} / {self.component.type}: "
            f"last_poll={self.last_poll_time}, total={self.accumulated_total}"
        )


class ComponentUserUsage(
    TimeStampedModel,
    core_models.DescribableMixin,
    core_models.BackendMixin,
    core_models.UuidMixin,
    LoggableMixin,
):
    """
    Per-user component usage tracking.

    Tracks component usage on a per-user basis for resources.
    Provides user-specific consumption data for detailed billing
    and usage analysis.
    """

    """
    This model represents an amount of a component consumed by a user.
    """

    user = models.ForeignKey["OfferingUser"](
        to="OfferingUser", on_delete=models.CASCADE, blank=True, null=True
    )
    username = models.CharField(max_length=100)
    component_usage = models.ForeignKey(ComponentUsage, on_delete=models.CASCADE)
    usage = models.DecimalField(default=0, decimal_places=2, max_digits=20)

    class Meta:
        unique_together = ("username", "component_usage")


class ComponentUserUsageLimit(
    TimeStampedModel,
    core_models.UuidMixin,
    LoggableMixin,
):
    """
    Per-user usage limits for components.

    Defines usage limits for individual users on specific components.
    Provides granular control over user consumption and resource access.
    """

    resource = models.ForeignKey(on_delete=models.CASCADE, to=Resource)
    component = models.ForeignKey(
        on_delete=models.CASCADE,
        to=OfferingComponent,
    )
    user = models.ForeignKey["OfferingUser"](
        to="OfferingUser", on_delete=models.CASCADE, null=False
    )
    limit = models.DecimalField(default=0, decimal_places=2, max_digits=20)

    class Meta:
        unique_together = ("resource", "component", "user")

    class Permissions:
        customer_path = ["resource__project__customer", "resource__offering__customer"]
        project_path = "resource__project"


class ComponentUsageMonthly(models.Model):
    """
    Denormalized reporting table summarizing component usage and limits per month.
    """

    component = models.ForeignKey(
        "OfferingComponent", on_delete=models.CASCADE, related_name="monthly_summaries"
    )
    billing_period = models.DateField(
        help_text="Always the first day of the month (e.g., 2023-10-01)"
    )

    # Pre-calculated aggregations
    total_consumed = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_allocated = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    usage_percent = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    class Meta:
        unique_together = ("component", "billing_period")
        indexes = [
            models.Index(fields=["billing_period", "component"]),
        ]


class OfferingFile(
    core_models.UuidMixin,
    core_models.NameMixin,
    TimeStampedModel,
):
    """
    File attachments for offerings.

    Provides file attachment functionality for offerings with
    upload management. Used for documentation, guides, and
    other supplementary materials.
    """

    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name="files"
    )
    file = models.FileField(upload_to="offering_files")

    class Permissions:
        customer_path = "offering__customer"

    @classmethod
    def get_url_name(cls):
        return "marketplace-offering-file"

    def __str__(self):
        return "offering: %s" % self.offering


class OfferingUser(
    TimeStampedModel,
    core_models.UuidMixin,
    common_mixins.BackendMetadataMixin,
    LoggableMixin,
):
    """
    User accounts within offerings.

    Manages user accounts with username mapping and restriction flags.
    Provides user management functionality for offering-specific
    access control and account management.
    """

    offering = models.ForeignKey(Offering, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    username = models.CharField(max_length=100, blank=True, null=True)
    is_restricted = models.BooleanField(
        default=False,
        help_text=_("Signal to service if the user account is restricted or not"),
    )
    state = FSMIntegerField(
        default=OfferingUserStates.CREATION_REQUESTED,
        choices=OfferingUserStates.CHOICES,
    )
    service_provider_comment = models.TextField(
        blank=True,
        help_text=_(
            "Additional comment for pending states like validation or account linking"
        ),
    )
    service_provider_comment_url = models.URLField(
        blank=True,
        help_text=_(
            "URL link for additional information or actions related to service provider comment"
        ),
    )
    tracker = cast(
        FieldInstanceTracker,
        FieldTracker(
            fields=[
                "username",
                "state",
                "is_restricted",
                "service_provider_comment",
                "service_provider_comment_url",
            ]
        ),
    )

    class Meta:
        unique_together = ("offering", "user")
        ordering = ["username"]
        indexes = [
            Index(fields=["offering", "user"], name="mp_offeringuser_offer_user_idx"),
        ]

    @transition(
        field=state,
        source=[
            OfferingUserStates.CREATION_REQUESTED,
            OfferingUserStates.ERROR_CREATING,
        ],
        target=OfferingUserStates.CREATING,
    )
    def begin_creating(self):
        pass

    @transition(
        field=state,
        source=[
            OfferingUserStates.CREATION_REQUESTED,
            OfferingUserStates.CREATING,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            OfferingUserStates.ERROR_CREATING,
            OfferingUserStates.ERROR_DELETING,
            OfferingUserStates.DELETION_REQUESTED,
            OfferingUserStates.DELETING,
        ],
        target=OfferingUserStates.OK,
    )
    def set_ok(self):
        pass

    @transition(
        field=state,
        source=[
            OfferingUserStates.CREATING,
            OfferingUserStates.ERROR_CREATING,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
        ],
        target=OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
    )
    def set_pending_additional_validation(self, comment=None, comment_url=None):
        if comment is not None:
            self.service_provider_comment = comment
        if comment_url is not None:
            self.service_provider_comment_url = comment_url

    @transition(
        field=state,
        source=[
            OfferingUserStates.CREATING,
            OfferingUserStates.ERROR_CREATING,
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        ],
        target=OfferingUserStates.PENDING_ACCOUNT_LINKING,
    )
    def set_pending_account_linking(self, comment=None, comment_url=None):
        if comment is not None:
            self.service_provider_comment = comment
        if comment_url is not None:
            self.service_provider_comment_url = comment_url

    @transition(
        field=state,
        source=[
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
        ],
        target=OfferingUserStates.OK,
    )
    def set_validation_complete(self):
        self.service_provider_comment = ""  # Clear comment when validation is complete
        self.service_provider_comment_url = (
            ""  # Clear comment URL when validation is complete
        )

    @transition(
        field=state,
        source=[
            OfferingUserStates.OK,
            OfferingUserStates.CREATING,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            OfferingUserStates.ERROR_CREATING,
        ],
        target=OfferingUserStates.DELETION_REQUESTED,
    )
    def request_deletion(self):
        pass

    @transition(
        field=state,
        source=[
            OfferingUserStates.DELETION_REQUESTED,
            OfferingUserStates.ERROR_DELETING,
        ],
        target=OfferingUserStates.DELETING,
    )
    def set_deleting(self):
        pass

    @transition(
        field=state,
        source=[
            OfferingUserStates.CREATION_REQUESTED,
            OfferingUserStates.DELETING,
        ],
        target=OfferingUserStates.DELETED,
    )
    def set_deleted(self):
        pass

    @transition(
        field=state,
        source=[
            OfferingUserStates.CREATION_REQUESTED,
            OfferingUserStates.CREATING,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
        ],
        target=OfferingUserStates.ERROR_CREATING,
    )
    def set_error_creating(self):
        pass

    @transition(
        field=state,
        source=[OfferingUserStates.DELETION_REQUESTED, OfferingUserStates.DELETING],
        target=OfferingUserStates.ERROR_DELETING,
    )
    def set_error_deleting(self):
        pass

    @transition(field=state, source="*", target=OfferingUserStates.ERROR_CREATING)
    def set_error(self):
        """Legacy method for backward compatibility - defaults to creation error."""
        pass

    def save(self, *args, **kwargs):
        # Set state to OK when username is known at creation time or when changed
        # This is triggered by:
        # 1. Creation with username (not self.pk)
        # 2. Direct username field updates (self.tracker.has_changed("username"))
        # 3. Signal handlers that call save() without update_fields (e.g., FreeIPA profile creation)
        if (
            self.username
            and self.state
            in (
                OfferingUserStates.CREATION_REQUESTED,
                OfferingUserStates.CREATING,
                OfferingUserStates.PENDING_ACCOUNT_LINKING,
                OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
                OfferingUserStates.ERROR_CREATING,
            )
            and (self.tracker.has_changed("username") or not self.pk)
        ):
            self.set_ok()
        super().save(*args, **kwargs)

    def get_log_fields(self):
        return (
            "offering",
            "user",
            "username",
            "is_restricted",
            "get_state_display",
            "service_provider_comment",
            "service_provider_comment_url",
        )

    def __str__(self) -> str:
        return f"{self.offering.name}: {self.username}"


class OfferingUserGroup(TimeStampedModel, common_mixins.BackendMetadataMixin):
    """
    User groups for offerings.

    Provides user group functionality for offerings with project
    associations. Used for managing group-based access control
    and permissions within offerings.
    """

    projects = models.ManyToManyField(structure_models.Project, blank=True)
    offering = models.ForeignKey(Offering, on_delete=models.CASCADE)

    def __str__(self):
        return "Offering user group for %s" % self.offering


class CategoryHelpArticle(models.Model):
    """
    Help documentation linked to categories.

    Provides help documentation with URL references linked to
    specific categories. Used for contextual help and documentation
    in the marketplace interface.
    """

    title = models.CharField(max_length=255, null=True, blank=True)
    url = models.URLField()
    categories = models.ManyToManyField(Category, related_name="articles", blank=True)

    def __str__(self):
        return self.title


class Tag(
    core_models.UuidMixin,
    TimeStampedModel,
):
    """Free-form tag for categorizing offerings."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=core_models.User,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("Tag")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "marketplace-tag"


class BaseServiceAccount(
    TimeStampedModel,
    core_models.UuidMixin,
    LoggableMixin,
    core_models.ErrorMessageMixin,
):
    """
    Abstract base class for service accounts.

    Provides common functionality for service accounts with username,
    description, and error handling. Used as base for different
    service account types.
    """

    username = models.CharField(max_length=32, blank=True)
    description = models.TextField(blank=True)
    state = FSMIntegerField(
        default=ServiceAccountState.OK,
        choices=ServiceAccountState.CHOICES,
    )
    get_state_display: Callable[[], Literal["OK", "Closed", "Erred"]]

    class Meta:
        abstract = True
        ordering = ["created"]

    def get_log_fields(self):
        return ("username",)

    @transition(
        field=state, source=ServiceAccountState.ERRED, target=ServiceAccountState.OK
    )
    def set_state_ok(self):
        pass

    @transition(
        field=state,
        source=[ServiceAccountState.OK, ServiceAccountState.ERRED],
        target=ServiceAccountState.CLOSED,
    )
    def set_state_closed(self):
        pass

    @transition(field=state, source="*", target=ServiceAccountState.ERRED)
    def set_state_erred(self):
        pass


class ScopedServiceAccount(BaseServiceAccount):
    """
    Abstract scoped service account.

    Extends BaseServiceAccount with email and identifier fields.
    Provides scoped service account functionality with additional
    user details and scope-specific management.
    """

    class Meta:
        abstract = True

    email = models.EmailField(max_length=320, default="")
    preferred_identifier = models.CharField(max_length=32, blank=True)
    scope: Any

    def __str__(self):
        return f"Service account {self.username} for {self.scope}"


class ProjectServiceAccount(ScopedServiceAccount):
    """
    Service account scoped to projects.

    Provides service account functionality scoped to specific projects.
    Used for project-level resource access management and automation.
    """

    project = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Project,
        null=True,
        blank=True,
    )

    tracker = cast(
        FieldInstanceTracker,
        FieldTracker(
            fields=["username", "email", "description", "preferred_identifier", "state"]
        ),
    )

    class Meta:
        verbose_name = _("Project service account")

    def __str__(self):
        return f"Project service account {self.username} for {self.project}"


class CustomerServiceAccount(ScopedServiceAccount):
    """
    Service account scoped to customers.

    Provides service account functionality scoped to specific customers.
    Used for organization-level access management and automation.
    """

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Customer,
        null=True,
        blank=True,
    )

    tracker = cast(
        FieldInstanceTracker,
        FieldTracker(
            fields=["username", "email", "description", "preferred_identifier", "state"]
        ),
    )

    class Meta:
        verbose_name = _("Customer service account")

    def __str__(self):
        return f"Customer service account {self.username} for {self.customer}"


class RobotAccount(
    BaseServiceAccount, common_mixins.BackendMetadataMixin, core_models.BackendMixin
):
    """
    Automated service account for resources.

    Provides automated service account functionality with state management,
    key storage, and user access control. Used for automated resource
    access and API integration.
    """

    resource = models.ForeignKey(Resource, on_delete=models.CASCADE)
    keys = models.JSONField(blank=True, default=list)
    get_state_display: Callable[[], str]
    responsible_user = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=User,
        null=True,
        blank=True,
        related_name="+",
    )

    # Get base fields from BaseServiceAccount's tracker and add new ones
    tracker = cast(
        FieldInstanceTracker,
        FieldTracker(fields=["username", "state", "resource", "type", "keys"]),
    )

    users = models.ManyToManyField(
        User, blank=True, help_text=_("Users who have access to this robot account.")
    )

    type = models.CharField(max_length=5, help_text=_("Type of the robot account."))

    state = FSMIntegerField(
        default=RobotAccountStates.REQUESTED, choices=RobotAccountStates.CHOICES
    )

    @transition(
        field=state,
        source=RobotAccountStates.REQUESTED,
        target=RobotAccountStates.CREATING,
    )
    def begin_creating(self):
        pass

    @transition(
        field=state,
        source=[RobotAccountStates.CREATING, RobotAccountStates.ERROR],
        target=RobotAccountStates.OK,
    )
    def set_ok(self):
        pass

    @transition(
        field=state,
        source=RobotAccountStates.OK,
        target=RobotAccountStates.REQUESTED_DELETION,
    )
    def request_deletion(self):
        pass

    @transition(
        field=state,
        source=RobotAccountStates.REQUESTED_DELETION,
        target=RobotAccountStates.DELETED,
    )
    def set_deleted(self):
        pass

    @transition(field=state, source="*", target=RobotAccountStates.ERROR)
    def set_error(self):
        pass

    class Meta:
        unique_together = ("resource", "type")
        ordering = ["created"]

    def get_log_fields(self):
        return super().get_log_fields() + ("type",)

    def __str__(self):
        return f"Robot account {self.username} for {self.resource}"


class OfferingAccessEndpoint(core_models.UuidMixin, core_models.NameMixin):
    """
    Access endpoints for offerings.

    Provides access endpoint functionality for offerings with URL management.
    Used for defining access points and integration endpoints for offerings.
    """

    url = core_fields.BackendURLField()
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name="endpoints"
    )


class ResourceAccessEndpoint(core_models.UuidMixin, core_models.NameMixin):
    """
    Access endpoints for resources.

    Provides access endpoint functionality for resources with URL management.
    Used for defining access points and integration endpoints for resources.
    """

    url = core_fields.BackendURLField()
    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name="endpoints"
    )


class OfferingUserRole(core_models.UuidMixin, core_models.NameMixin):
    """
    User roles within offerings.

    Defines user roles for offerings providing permission management
    and access control. Used for role-based access control within
    offering contexts.
    """

    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, related_name="roles"
    )
    scope_type = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=_(
            "Level this role applies at, e.g. 'cluster', 'project'. "
            "Empty means offering-wide."
        ),
    )
    scope_type_label = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text=_(
            "Human-readable label for scope_type shown to end users, "
            "e.g. 'Rancher Project', 'Cluster Namespace'. "
            "Falls back to capitalized scope_type if empty."
        ),
    )

    class Meta:
        ordering = ["name"]


class SoftwareCatalog(core_models.UuidMixin, TimeStampedModel):
    """
    Generic software catalog supporting multiple package management systems.

    Supports various catalog types:
    - binary_runtime: Pre-compiled software (EESSI)
    - source_package: Build recipes (Spack)
    - package_manager: Package manager repos (conda-forge, PyPI)
    """

    CATALOG_TYPE_CHOICES = [
        ("binary_runtime", _("Binary Runtime (EESSI)")),
        ("source_package", _("Source Package (Spack)")),
        ("package_manager", _("Package Manager (conda, pip)")),
    ]

    name = models.CharField(
        max_length=100, help_text=_("Catalog name (e.g., EESSI, Spack)")
    )
    version = models.CharField(
        max_length=50, help_text=_("Catalog version (e.g., 2023.06, 0.21.0)")
    )
    catalog_type = models.CharField(
        max_length=50,
        choices=CATALOG_TYPE_CHOICES,
        default="binary_runtime",  # Default to EESSI-like for existing catalogs
        help_text=_("Type of software catalog"),
    )
    source_url = models.URLField(blank=True, help_text=_("Catalog source URL"))
    description = models.TextField(blank=True)

    # Flexible storage for catalog-specific metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Catalog-specific metadata (architecture maps, API endpoints, etc.)"
        ),
    )

    # Auto-update configuration
    auto_update_enabled = models.BooleanField(
        default=True,
        help_text=_("Whether to automatically update this catalog via scheduled tasks"),
    )
    last_update_attempt = models.DateTimeField(null=True, blank=True)
    last_successful_update = models.DateTimeField(null=True, blank=True)
    update_errors = models.TextField(blank=True)

    class Meta:
        unique_together = ("name", "version", "catalog_type")
        ordering = ["name", "catalog_type", "version"]

    def __str__(self):
        return f"{self.name} {self.version} ({self.get_catalog_type_display()})"


class SoftwarePackage(core_models.UuidMixin, TimeStampedModel):
    """
    Generic software package supporting multiple catalog types.

    Includes common fields across EESSI, Spack, and other package systems.
    """

    catalog = models.ForeignKey(
        SoftwareCatalog, on_delete=models.CASCADE, related_name="packages"
    )
    name = models.CharField(max_length=200, db_index=True)
    description = models.TextField(blank=True)
    homepage = models.URLField(blank=True, null=True)

    # Generic classification fields (common across catalogs)
    categories = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Package categories (e.g., ['bio', 'hpc', 'build-tools'])"),
    )
    licenses = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Software licenses (e.g., ['GPL-3.0', 'MIT'])"),
    )
    maintainers = models.JSONField(
        default=list, blank=True, help_text=_("Package maintainers")
    )

    # Extension/hierarchy support (for Python packages, R packages, etc.)
    is_extension = models.BooleanField(
        default=False,
        help_text=_("Whether this package is an extension of another package"),
    )
    parent_softwares = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="extensions",
        help_text=_(
            "Parent packages for extensions (e.g., Python package within Python)"
        ),
    )

    class Meta:
        unique_together = ("catalog", "name")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["catalog", "name"]),
            models.Index(fields=["name"]),
            models.Index(fields=["is_extension"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.catalog})"

    @property
    def extension_count(self):
        """Count of extension packages."""
        return self.extensions.count()


class SoftwareVersion(core_models.UuidMixin, TimeStampedModel):
    """
    Generic software version supporting multiple catalog types.

    Stores common version metadata and catalog-specific data in flexible fields.
    """

    package = models.ForeignKey(
        SoftwarePackage, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.CharField(max_length=100)
    release_date = models.DateField(null=True, blank=True)

    # Generic dependency tracking (catalog-agnostic)
    dependencies = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Package dependencies (format varies by catalog type)"),
    )

    # Flexible metadata storage for all catalog-specific data
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Version-specific metadata (toolchains, build info, modules, etc.)"
        ),
    )

    class Meta:
        unique_together = ("package", "version")
        ordering = ["package", "version"]
        indexes = [
            models.Index(fields=["package", "version"]),
            models.Index(fields=["version"]),
        ]

    def __str__(self):
        return f"{self.package.name} {self.version}"

    @property
    def catalog_type(self):
        """Return the catalog type for this version."""
        return self.package.catalog.catalog_type

    def get_metadata_field(self, field_name, default=None):
        """Helper to get version metadata fields."""
        return self.metadata.get(field_name, default)


class SoftwareTarget(core_models.UuidMixin, TimeStampedModel):
    """
    Generic deployment target for software versions.

    Represents where/how software can be deployed - architecture for binary catalogs,
    build variants for source catalogs, etc.
    """

    version = models.ForeignKey(
        SoftwareVersion, on_delete=models.CASCADE, related_name="targets"
    )

    # Generic target identification
    target_type = models.CharField(
        max_length=50,
        db_index=True,
        default="architecture",
        help_text=_("Type of target (architecture, platform, variant, etc.)"),
    )
    target_name = models.CharField(
        max_length=100,
        db_index=True,
        default="generic",
        help_text=_("Target identifier (x86_64/generic, linux, variant_name, etc.)"),
    )

    # Optional secondary classification (for hierarchical targets)
    target_subtype = models.CharField(
        max_length=50,
        blank=True,
        db_index=True,
        help_text=_("Target subtype (microarchitecture, distribution, etc.)"),
    )

    # Generic path/location (filesystem path, URL, etc.)
    location = models.CharField(
        max_length=500,
        blank=True,
        help_text=_("Target location (CVMFS path, download URL, etc.)"),
    )

    # Flexible metadata for target-specific data
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Target-specific metadata (build options, system requirements, etc.)"
        ),
    )

    gpu_architectures = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of GPU architectures this target supports (e.g., ['nvidia/cc70', 'nvidia/cc90'])"
        ),
    )

    class Meta:
        unique_together = ("version", "target_type", "target_name", "target_subtype")
        indexes = [
            models.Index(fields=["target_type", "target_name"]),
            models.Index(fields=["version", "target_type"]),
            models.Index(fields=["target_type", "target_subtype"]),
        ]

    def __str__(self):
        if self.target_subtype:
            return f"{self.version} - {self.target_type}:{self.target_name}/{self.target_subtype}"
        return f"{self.version} - {self.target_type}:{self.target_name}"


class OfferingSoftwareCatalog(core_models.UuidMixin, TimeStampedModel):
    """
    Link between offerings and software catalogs with enabled targets.
    """

    offering = models.ForeignKey(
        "Offering", on_delete=models.CASCADE, related_name="software_catalogs"
    )
    catalog = models.ForeignKey(
        SoftwareCatalog, on_delete=models.CASCADE, related_name="offerings"
    )
    enabled_cpu_family = models.JSONField(
        default=list,
        help_text=_("List of enabled CPU families: ['x86_64', 'aarch64']"),
    )
    enabled_cpu_microarchitectures = models.JSONField(
        default=list,
        help_text=_("List of enabled CPU microarchitectures: ['generic', 'zen3']"),
    )
    partition = models.ForeignKey(
        "OfferingPartition",
        on_delete=models.CASCADE,
        related_name="software_catalogs",
        null=True,
        blank=True,
        help_text=_(
            "Optional SLURM partition this software catalog is associated with"
        ),
    )

    class Meta:
        unique_together = ("offering", "catalog")

    def __str__(self):
        return f"{self.offering.name} - {self.catalog}"


class OfferingPartition(core_models.UuidMixin, TimeStampedModel):
    """
    SLURM partition configuration for offerings.

    Stores SLURM partition information when an offering represents a SLURM cluster.
    Maps to SLURM partition_info_t struct fields.
    """

    offering = models.ForeignKey(
        Offering, on_delete=models.CASCADE, related_name="partitions"
    )

    # Basic partition info
    partition_name = models.CharField(
        max_length=255, help_text=_("Name of the SLURM partition")
    )

    # CPU configuration
    cpu_bind = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("Default task binding policy (SLURM cpu_bind)"),
    )
    def_cpu_per_gpu = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Default CPUs allocated per GPU")
    )
    max_cpus_per_node = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Maximum allocated CPUs per node")
    )
    max_cpus_per_socket = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Maximum allocated CPUs per socket")
    )

    # Memory configuration (in MB)
    def_mem_per_cpu = models.PositiveBigIntegerField(
        null=True, blank=True, help_text=_("Default memory per CPU in MB")
    )
    def_mem_per_gpu = models.PositiveBigIntegerField(
        null=True, blank=True, help_text=_("Default memory per GPU in MB")
    )
    def_mem_per_node = models.PositiveBigIntegerField(
        null=True, blank=True, help_text=_("Default memory per node in MB")
    )
    max_mem_per_cpu = models.PositiveBigIntegerField(
        null=True, blank=True, help_text=_("Maximum memory per CPU in MB")
    )
    max_mem_per_node = models.PositiveBigIntegerField(
        null=True, blank=True, help_text=_("Maximum memory per node in MB")
    )

    # Time limits (in minutes)
    default_time = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Default time limit in minutes")
    )
    max_time = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Maximum time limit in minutes")
    )
    grace_time = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Preemption grace time in seconds")
    )

    # Node configuration
    max_nodes = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Maximum nodes per job")
    )
    min_nodes = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Minimum nodes per job")
    )

    # Resource exclusivity
    exclusive_topo = models.BooleanField(
        default=False, help_text=_("Exclusive topology access required")
    )
    exclusive_user = models.BooleanField(
        default=False, help_text=_("Exclusive user access required")
    )

    # Architecture
    cpu_arch = models.CharField(
        max_length=100,
        blank=True,
        help_text=_("CPU architecture of the partition (e.g., x86_64/amd/zen3)"),
    )
    gpu_arch = models.CharField(
        max_length=100,
        blank=True,
        help_text=_(
            "GPU architecture of the partition (e.g., nvidia/cc90, amd/gfx90a)"
        ),
    )

    # Scheduling configuration
    priority_tier = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=_("Priority tier for scheduling and preemption"),
    )
    qos = models.CharField(
        max_length=255, blank=True, help_text=_("Quality of Service (QOS) name")
    )
    req_resv = models.BooleanField(
        default=False, help_text=_("Require reservation for job allocation")
    )

    class Meta:
        unique_together = ("offering", "partition_name")
        indexes = [
            models.Index(fields=["offering", "partition_name"]),
            models.Index(fields=["priority_tier"]),
        ]

    def __str__(self):
        return f"{self.offering.name} - {self.partition_name}"


class ResourceUser(TimeStampedModel, core_models.UuidMixin):
    """
    User-role assignments for resources.

    Manages user-role assignments for resources with timestamp tracking.
    Provides fine-grained access control and permission management
    for individual resources.
    """

    resource = models.ForeignKey(
        on_delete=models.CASCADE, to=Resource, related_name="users"
    )
    user = models.ForeignKey(
        core_models.User, related_name="+", on_delete=models.CASCADE
    )
    role = models.ForeignKey(
        OfferingUserRole, related_name="+", on_delete=models.CASCADE
    )

    class Meta:
        ordering = ["created"]

    def get_log_fields(self):
        return (
            "resource",
            "user",
            "role",
        )


class IntegrationStatus(core_models.UuidMixin):
    """
    Backend integration status tracking.

    Tracks integration status with different agent types and connection
    monitoring. Provides visibility into backend service health and
    connectivity status.
    """

    class States:
        UNKNOWN = 1
        ACTIVE = 2
        DISCONNECTED = 3

        CHOICES = (
            (UNKNOWN, "Unknown"),
            (ACTIVE, "Active"),
            (DISCONNECTED, "Disconnected"),
        )

    class AgentTypes:
        ORDER_PROCESSING = 1
        USAGE_REPORTING = 2
        GLAUTH_SYNC = 3
        RESOURCE_SYNC = 4
        EVENT_PROCESSING = 5

        CHOICES = (
            (ORDER_PROCESSING, "Order processing"),
            (USAGE_REPORTING, "Usage reporting"),
            (GLAUTH_SYNC, "Glauth sync"),
            (RESOURCE_SYNC, "Resource sync"),
            (EVENT_PROCESSING, "Event processing"),
        )

    agent_type = models.CharField(
        max_length=20, choices=AgentTypes.CHOICES, default=AgentTypes.ORDER_PROCESSING
    )
    offering = models.ForeignKey(
        Offering,
        on_delete=models.CASCADE,
    )
    status = FSMIntegerField(default=States.UNKNOWN, choices=States.CHOICES)
    last_request_timestamp = models.DateTimeField(
        _("time of latest backend request"), null=True, blank=True, editable=False
    )
    service_name = models.CharField(_("Service name"), max_length=150, default="")

    class Meta:
        unique_together = ("offering", "agent_type")

    @transition(
        field=status,
        source="*",
        target=States.ACTIVE,
    )
    def set_backend_active(self):
        pass

    @transition(
        field=status,
        source=States.ACTIVE,
        target=States.DISCONNECTED,
    )
    def set_backend_disconnected(self):
        pass

    def set_last_request_timestamp(self):
        self.last_request_timestamp = timezone.now()


class BackendResource(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.BackendMixin,
    TimeStampedModel,
    common_mixins.BackendMetadataMixin,
):
    """
    Backend resource representation for import capabilities.

    Represents resources in external backends that can be imported
    into the marketplace. Provides metadata and identification for
    backend resource discovery and import processes.
    """

    """This model represents a resource in backend, which could be imported."""

    offering = models.ForeignKey(to=Offering, on_delete=models.CASCADE)
    project = models.ForeignKey(to=structure_models.Project, on_delete=models.CASCADE)


class BackendResourceRequest(
    core_models.UuidMixin, TimeStampedModel, core_models.ErrorMessageMixin
):
    """
    Backend resource request processing.

    Manages backend resource requests with state management and
    timing tracking. Provides request lifecycle management for
    backend operations and resource processing.
    """

    class States:
        SENT = "Sent"
        PROCESSING = "Processing"
        DONE = "Done"
        ERRED = "Erred"

        CHOICES = (
            (SENT, SENT),
            (PROCESSING, PROCESSING),
            (DONE, DONE),
            (ERRED, ERRED),
        )

    started = models.DateTimeField(
        blank=True, null=True, help_text=_("Time when request processing started")
    )
    finished = models.DateTimeField(
        blank=True, null=True, help_text=_("Time when request processing finished")
    )

    state = FSMField(choices=States.CHOICES, default=States.SENT)
    offering = models.ForeignKey(to=Offering, on_delete=models.CASCADE)

    @transition(field=state, source=States.SENT, target=States.PROCESSING)
    def start_processing(self):
        self.started = timezone.now()

    @transition(field=state, source=States.PROCESSING, target=States.DONE)
    def set_done(self):
        self.finished = timezone.now()

    @transition(field=state, source="*", target=States.ERRED)
    def set_erred(self):
        self.finished = timezone.now()


reversion.register(Screenshot)
reversion.register(OfferingComponent)
reversion.register(PlanComponent)
reversion.register(Plan, follow=("components",))
reversion.register(Offering, follow=("components", "plans", "screenshots"))
reversion.register(
    Resource,
    fields=[
        # Business-relevant fields to track:
        "name",
        "description",
        "slug",
        "state",
        "limits",
        "attributes",
        "options",
        "cost",
        "end_date",
        "downscaled",
        "restrict_member_access",
        "paused",
        # Note: plan_id is tracked to capture plan switches
        "plan",
    ],
)


class MaintenanceAnnouncement(
    core_models.UuidMixin,
    core_models.NameMixin,
    TimeStampedModel,
    LoggableMixin,
    core_models.BackendMixin,
):
    message = models.CharField(_("message"), max_length=2000, blank=True)
    internal_notes = models.CharField(_("internal notes"), max_length=2000, blank=True)
    maintenance_type = models.PositiveSmallIntegerField(
        choices=MaintenanceType.CHOICES,
        default=MaintenanceType.SCHEDULED,
        help_text=_("Type of maintenance being performed"),
    )

    state = FSMIntegerField(
        default=MaintenanceState.DRAFT, choices=MaintenanceState.CHOICES
    )

    scheduled_start = models.DateTimeField(
        help_text=_("When the maintenance is scheduled to begin")
    )
    scheduled_end = models.DateTimeField(
        help_text=_("When the maintenance is scheduled to complete")
    )
    actual_start = models.DateTimeField(
        null=True, blank=True, help_text=_("When the maintenance actually began")
    )
    actual_end = models.DateTimeField(
        null=True, blank=True, help_text=_("When the maintenance actually completed")
    )

    service_provider = models.ForeignKey(
        ServiceProvider,
        on_delete=models.CASCADE,
        related_name="maintenance_announcements",
        help_text=_("Service provider announcing the maintenance"),
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_maintenance_announcements",
    )

    external_reference_url = models.URLField(
        blank=True, help_text="Optional reference to an external maintenance tracker"
    )
    admin_announcement = models.OneToOneField(
        notifications_models.AdminAnnouncement,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="maintenance_announcement",
        help_text="Associated admin announcement for this maintenance",
    )
    tracker = FieldTracker()

    class Meta:
        verbose_name = _("Maintenance announcement")
        verbose_name_plural = _("Maintenance announcements")
        ordering = ["-scheduled_start"]

    class Permissions:
        customer_path = "service_provider__customer"

    @property
    def affected_offerings_count(self):
        """Count of affected offerings"""
        return self.affected_offerings.count()

    @transition(
        field=state, source=MaintenanceState.DRAFT, target=MaintenanceState.SCHEDULED
    )
    def schedule(self):
        """Publish the maintenance announcement"""
        pass

    @transition(
        field=state, source=MaintenanceState.SCHEDULED, target=MaintenanceState.DRAFT
    )
    def unschedule(self):
        """Unpublish the maintenance announcement"""
        pass

    @transition(
        field=state,
        source=MaintenanceState.SCHEDULED,
        target=MaintenanceState.IN_PROGRESS,
    )
    def start_maintenance(self):
        """Mark maintenance as started"""
        if not self.actual_start:
            self.actual_start = timezone.now()

    @transition(
        field=state,
        source=MaintenanceState.IN_PROGRESS,
        target=MaintenanceState.COMPLETED,
    )
    def complete_maintenance(self):
        """Mark maintenance as completed"""
        if not self.actual_end:
            self.actual_end = timezone.now()

    @transition(
        field=state,
        source=[MaintenanceState.DRAFT, MaintenanceState.SCHEDULED],
        target=MaintenanceState.CANCELLED,
    )
    def cancel_maintenance(self):
        """Cancel the maintenance"""
        pass

    def get_log_fields(self):
        return (
            "uuid",
            "title",
            "maintenance_type",
            "state",
            "scheduled_start",
            "scheduled_end",
            "service_provider",
            "created_by",
        )

    def __str__(self):
        return f"{self.name} - {self.state}"


class MaintenanceAnnouncementOffering(core_models.UuidMixin, TimeStampedModel):
    maintenance = models.ForeignKey(
        MaintenanceAnnouncement,
        on_delete=models.CASCADE,
        related_name="affected_offerings",
    )
    offering = models.ForeignKey(
        Offering, on_delete=models.CASCADE, related_name="maintenance_announcements"
    )
    impact_level = models.PositiveSmallIntegerField(
        choices=ImpactLevel.CHOICES,
        default=ImpactLevel.DEGRADED_PERFORMANCE,
        help_text=_("Expected impact on this offering"),
    )
    impact_description = models.TextField(
        blank=True,
        help_text=_("Specific description of how this offering will be affected"),
    )

    class Meta:
        unique_together = ("maintenance", "offering")
        verbose_name = _("Maintenance affected offering")
        verbose_name_plural = _("Maintenance affected offerings")

    class Permissions:
        customer_path = "maintenance__service_provider__customer"

    def __str__(self):
        return f"{self.maintenance.name} affects {self.offering.name}"


class MaintenanceAnnouncementTemplate(
    core_models.UuidMixin, core_models.NameMixin, TimeStampedModel
):
    message = models.CharField(_("message"), max_length=2000, blank=True)
    maintenance_type = models.PositiveSmallIntegerField(
        choices=MaintenanceType.CHOICES,
        default=MaintenanceType.SCHEDULED,
        help_text=_("Type of maintenance being performed"),
    )

    service_provider = models.ForeignKey(
        ServiceProvider,
        on_delete=models.CASCADE,
        related_name="+",
        help_text=_("Service provider announcing the maintenance"),
    )

    tracker = FieldTracker()

    class Meta:
        verbose_name = _("Maintenance announcement")
        verbose_name_plural = _("Maintenance announcements")
        ordering = ["-created"]

    class Permissions:
        customer_path = "service_provider__customer"

    def __str__(self):
        return f"{self.name} - {self.get_maintenance_type_display()}"


class MaintenanceAnnouncementOfferingTemplate(core_models.UuidMixin, TimeStampedModel):
    maintenance_template = models.ForeignKey(
        MaintenanceAnnouncementTemplate,
        on_delete=models.CASCADE,
        related_name="affected_offerings",
    )
    offering = models.ForeignKey(Offering, on_delete=models.CASCADE, related_name="+")
    impact_level = models.PositiveSmallIntegerField(
        choices=ImpactLevel.CHOICES,
        default=ImpactLevel.DEGRADED_PERFORMANCE,
        help_text=_("Expected impact on this offering"),
    )
    impact_description = models.TextField(
        blank=True,
        help_text=_("Specific description of how this offering will be affected"),
    )

    class Meta:
        unique_together = ("maintenance_template", "offering")
        verbose_name = _("Maintenance affected offering")
        verbose_name_plural = _("Maintenance affected offerings")

    class Permissions:
        customer_path = "maintenance_template__service_provider__customer"

    def __str__(self):
        return f"{self.maintenance_template.name} affects {self.offering.name}"


class CourseAccount(
    core_models.UuidMixin,
    TimeStampedModel,
    LoggableMixin,
    core_models.ErrorMessageMixin,
    core_models.DescribableMixin,
):
    project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        to=core_models.User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    state = FSMIntegerField(
        default=CourseAccountState.OK,
        choices=CourseAccountState.choices,
    )
    email = models.EmailField(max_length=320, default="")
    tracker = cast(
        FieldInstanceTracker,
        FieldTracker(fields=["email", "description", "state", "user"]),
    )
    get_state_display: Callable[[], Literal["OK", "Closed", "Erred"]]

    class Meta:
        verbose_name = _("Course account")
        ordering = ["created"]

    def __str__(self):
        user_name = self.user.username if self.user else "unknown"
        return f"Course account for {user_name} in {self.project}"

    @transition(
        field=state,
        source=[CourseAccountState.ERRED, CourseAccountState.PENDING],
        target=CourseAccountState.OK,
    )
    def set_state_ok(self):
        pass

    @transition(
        field=state,
        source=[CourseAccountState.OK, CourseAccountState.ERRED],
        target=CourseAccountState.CLOSED,
    )
    def set_state_closed(self):
        pass

    @transition(field=state, source="*", target=CourseAccountState.ERRED)
    def set_state_erred(self):
        pass

    @transition(field=state, source="*", target=CourseAccountState.PENDING)
    def set_state_pending(self):
        pass
