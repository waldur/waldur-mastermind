import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.conf import settings as django_settings
from django.contrib.auth.models import PermissionsMixin, UserManager
from django.core import validators
from django.db import models, transaction
from django.template.defaultfilters import slugify
from django.utils import timezone as django_timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import ConcurrentTransitionMixin, FSMIntegerField, transition
from model_utils import FieldTracker
from model_utils.fields import AutoLastModifiedField
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from reversion import revisions as reversion

from waldur_core.core import managers as core_managers
from waldur_core.core.enums import GENDER_CHOICES as _GENDER_CHOICES_RAW
from waldur_core.core.enums import CoreStates
from waldur_core.core.fields import JSONField, UUIDField
from waldur_core.core.utils import normalize_unicode, send_mail
from waldur_core.core.validators import (
    is_potentially_dangerous_regex,
    normalize_network_acl,
    validate_gender,
    validate_iso_3166_alpha2,
    validate_name,
    validate_nationalities,
    validate_personal_title,
    validate_phone_number,
    validate_refeds_assurance_list,
    validate_schac_organization_type,
    validate_ssh_public_key,
)
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.media.mixins import ImageModelMixin

from .shims import AbstractBaseUser

logger = logging.getLogger(__name__)


DESCRIPTION_LENGTH = 4096

NAME_LENGTH = 150

USERNAME_REGEX = r"^[a-zA-Z0-9_.][a-zA-Z0-9_.-]*[a-zA-Z0-9_.$-]?$"

GENDER_CHOICES = [(code, _(label)) for code, label in _GENDER_CHOICES_RAW]


class DescribableMixin(models.Model):
    """
    Mixin to add a standardized "description" field.
    """

    class Meta:
        abstract = True

    description = models.CharField(
        _("description"), max_length=DESCRIPTION_LENGTH, blank=True
    )


class NameMixin(models.Model):
    """
    Mixin to add a standardized "name" field with validation.

    Provides a CharField with maximum length of 150 characters and
    validates the name using the validate_name validator.
    """

    class Meta:
        abstract = True

    name = models.CharField(
        _("name"), max_length=NAME_LENGTH, validators=[validate_name]
    )


SLUG_NAME_LIMIT = 10


class SlugMixin(models.Model):
    """
    Mixin to automatically generate a name-based slug.

    Generates unique slugs based on the source field (default: 'name')
    during save operations. Uses generate_slug() to ensure uniqueness
    by appending numeric suffixes when needed.
    """

    slug = models.SlugField()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.generate_slug()

        super().save(*args, **kwargs)

    def generate_slug(self):
        slug_source = getattr(self, self.get_slug_source_field())
        return generate_slug(slug_source, self.__class__)

    @classmethod
    def get_slug_source_field(cls):
        return "name"


def generate_slug(name, klass):
    """
    Generate a unique slug for a model instance.

    Creates a slug from the given name and ensures uniqueness by
    appending numeric suffixes if conflicts exist.

    Args:
        name: The source name to generate slug from
        klass: The model class to check for existing slugs

    Returns:
        A unique slug string
    """
    base_slug = clean_slug_hyphens(slugify(name)[:SLUG_NAME_LIMIT])

    existing_slugs = klass.objects.filter(slug__startswith=base_slug).values_list(
        "slug", flat=True
    )

    # If base slug is available, return it
    if base_slug not in existing_slugs:
        return base_slug

    # Find maximum suffix for numbered slugs
    max_num = 1  # Start from 1, so next available will be 2
    for slug in existing_slugs:
        if slug == base_slug:
            continue  # Skip the base slug itself
        try:
            num = int(slug.split("-")[-1])
            if num > max_num:
                max_num = num
        except ValueError:
            pass

    return f"{base_slug}-{max_num + 1}"


def clean_slug_hyphens(slug: str) -> str:
    """
    Clean duplicate hyphens from a slug.

    Replaces multiple consecutive hyphens with a single hyphen
    and removes leading/trailing hyphens.

    Args:
        slug: The slug string to clean

    Returns:
        A cleaned slug string with no duplicate hyphens
    """
    # Replace multiple consecutive hyphens with single hyphen
    cleaned = re.sub(r"-+", "-", slug)
    # Remove leading and trailing hyphens
    cleaned = cleaned.strip("-")
    return cleaned


class UiDescribableMixin(DescribableMixin):
    """
    Mixin to add a standardized "description" and "icon url" fields.

    Extends DescribableMixin with an icon_url field for UI display purposes.
    The icon_url field accepts URLs up to 500 characters.
    """

    class Meta:
        abstract = True

    icon_url = models.URLField(_("icon url"), max_length=500, blank=True)


class UuidMixin(models.Model):
    """
    Mixin to identify models by UUID.

    Provides a UUID field for unique model identification.
    The UUID is automatically generated and used as a primary identifier.
    """

    class Meta:
        abstract = True

    uuid: UUID = UUIDField()


class ErrorMessageMixin(models.Model):
    """
    Mixin to add standardized error handling fields.

    Provides error_message and error_traceback TextField for storing
    error information and debugging details when operations fail.
    """

    class Meta:
        abstract = True

    error_message = models.TextField(blank=True)
    error_traceback = models.TextField(blank=True)


class LastSyncMixin(models.Model):
    """
    Mixin to track last synchronization time.

    Provides a last_sync DateTimeField that defaults to the current time
    and is not editable through forms. Used for tracking when data was
    last synchronized with external systems.
    """

    class Meta:
        abstract = True

    last_sync = models.DateTimeField(default=django_timezone.now, editable=False)


class UserDetailsMixin(models.Model):
    """
    This mixin is shared by User and Invitation model. All fields are optional.
    User is populated with these details when invitation is approved.
    Note that civil_number and email fields are not included in this mixin
    because they have different constraints in User and Invitation model.
    """

    class Meta:
        abstract = True

    native_name = models.CharField(_("native name"), max_length=100, blank=True)
    phone_number = models.CharField(
        _("phone number"),
        max_length=255,
        blank=True,
        validators=[validate_phone_number],
    )
    organization = models.CharField(_("organization"), max_length=255, blank=True)
    job_title = models.CharField(_("job title"), max_length=120, blank=True)
    affiliations = models.JSONField(
        default=list,
        blank=True,
        help_text="Person's affiliation within organization such as student, faculty, staff.",
    )

    def _process_saml2_affiliations(self, affiliations) -> bool:
        """
        Due to djangosaml2 assumption that attributes list should have at most one element
        we have to implement custom method to process affiliations fetched from SAML2 IdP.
        See also: https://github.com/IdentityPython/djangosaml2/issues/28
        Return true to indicate if value has been changed or not.
        """
        if self.affiliations != affiliations:
            self.affiliations = affiliations
            return True
        return False


@reversion.register()
class User(
    SlugMixin,
    LoggableMixin,
    UuidMixin,
    LastSyncMixin,
    DescribableMixin,
    AbstractBaseUser,
    UserDetailsMixin,
    PermissionsMixin,
    ImageModelMixin,
):
    """
    Main user model with comprehensive user management.

    Provides authentication, user profile details, permissions,
    and image support. Includes methods for email handling,
    permission checking, and change request management.

    Inherits from multiple mixins to provide UUID identification,
    logging capabilities, slug generation, and user detail fields.
    """

    id: int

    username = models.CharField(
        _("username"),
        max_length=128,
        unique=True,
        help_text=_(
            "Required. 128 characters or fewer. Lowercase letters, numbers and "
            "@/./+/-/_ characters"
        ),
        validators=[
            validators.RegexValidator(
                re.compile(r"^[0-9a-z_.@+-]+$"), _("Enter a valid username."), "invalid"
            )
        ],
    )
    # Civil number is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    civil_number = models.CharField(
        _("civil number"),
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        default=None,
    )
    email = models.EmailField(_("email address"), max_length=320, blank=True)

    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Designates whether the user can log into this admin site."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as "
            "active. Unselect this instead of deleting accounts."
        ),
    )
    is_support = models.BooleanField(
        _("support status"),
        default=False,
        help_text=_("Designates whether the user is a global support user."),
    )
    is_identity_manager = models.BooleanField(
        default=False,
        help_text=_(
            "Designates whether the user is allowed to manage remote user identities."
        ),
    )
    can_use_personal_access_tokens = models.BooleanField(
        default=False,
        help_text=_(
            "Designates whether the user is allowed to create and use "
            "personal access tokens."
        ),
    )
    deactivation_reason = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=_(
            "Reason why the user was deactivated. Visible to staff and support."
        ),
    )
    is_admin_deactivated = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_(
            "Designates that the user was deactivated by an administrator and "
            "must not be reactivated automatically by the role-sync task. "
            "Visible to staff and support."
        ),
    )
    notifications_enabled = models.BooleanField(
        default=True,
        help_text=_(
            "Designates whether the user is allowed to receive email notifications."
        ),
    )
    date_joined = models.DateTimeField(_("date joined"), default=django_timezone.now)
    modified = AutoLastModifiedField(_("modified"))
    registration_method = models.CharField(
        _("registration method"),
        max_length=50,
        default="default",
        blank=True,
        help_text=_("Indicates what registration method was used."),
    )
    identity_source = models.CharField(
        _("source of identity"),
        max_length=50,
        default="",
        blank=True,
        help_text=_("Indicates what identity provider was used."),
    )
    uid_number = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "POSIX UID from the identity provider; used when an offering's "
            "uid_source is 'user_attribute'."
        ),
    )
    primary_gid = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "POSIX primary GID from the identity provider; used when an "
            "offering's gid_source is 'user_attribute'."
        ),
    )
    agreement_date = models.DateTimeField(
        _("agreement date"),
        blank=True,
        null=True,
        help_text=_("Indicates when the user has agreed with the policy."),
    )
    preferred_language = models.CharField(max_length=10, blank=True)
    token_lifetime = models.PositiveIntegerField(
        null=True,
        help_text=_("Token lifetime in seconds."),
        validators=[validators.MinValueValidator(60)],
    )
    details = models.JSONField(
        blank=True,
        default=dict,
        help_text=_("Extra details from authentication backend."),
    )
    backend_id = models.CharField(max_length=255, blank=True)
    first_name = models.CharField(_("first name"), max_length=100, blank=True)
    last_name = models.CharField(_("last name"), max_length=100, blank=True)
    birth_date = models.DateField(_("birth date"), null=True, blank=True)

    # Identity Bridge fields
    attribute_sources = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Per-attribute source and freshness tracking. "
            "Format: {'field_name': {'source': 'isd:<name>', 'timestamp': 'ISO8601'}}."
        ),
    )
    managed_isds = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of ISD source identifiers this user can manage via Identity Bridge. "
            "E.g., ['isd:puhuri', 'isd:fenix']. Non-empty list implies identity manager role."
        ),
    )
    active_isds = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of ISDs that have asserted this user exists. "
            "User is deactivated when this becomes empty."
        ),
    )

    # AAI (Authentication and Authorization Infrastructure) attributes
    # Personal identity (from passport/IdP)
    gender = models.CharField(
        _("gender"),
        max_length=10,
        null=True,
        blank=True,
        choices=GENDER_CHOICES,
        validators=[validate_gender],
        help_text=_("User's gender (male, female, or unknown)"),
    )
    personal_title = models.CharField(
        _("personal title"),
        max_length=50,
        blank=True,
        validators=[validate_personal_title],
        help_text=_("Honorific title (Mr, Ms, Dr, Prof, etc.)"),
    )
    place_of_birth = models.CharField(
        _("place of birth"),
        max_length=255,
        blank=True,
    )
    address = models.CharField(
        _("address"),
        max_length=255,
        blank=True,
    )

    # Geographic (ISO 3166-1 alpha-2)
    country_of_residence = models.CharField(
        _("country of residence"),
        max_length=2,
        blank=True,
        validators=[validate_iso_3166_alpha2],
    )
    nationality = models.CharField(
        _("nationality"),
        max_length=2,
        blank=True,
        validators=[validate_iso_3166_alpha2],
        help_text=_("Primary citizenship (ISO 3166-1 alpha-2 code)"),
    )
    nationalities = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_nationalities],
        help_text=_("List of all citizenships (ISO 3166-1 alpha-2 codes)"),
    )

    # Organization extended
    organization_country = models.CharField(
        _("organization country"),
        max_length=2,
        blank=True,
        validators=[validate_iso_3166_alpha2],
    )
    organization_type = models.CharField(
        _("organization type"),
        max_length=255,
        blank=True,
        validators=[validate_schac_organization_type],
        help_text=_("SCHAC URN (e.g., urn:schac:homeOrganizationType:int:university)"),
    )
    organization_registry_code = models.CharField(
        _("organization registry code"),
        max_length=255,
        blank=True,
        help_text=_("Company registration code of the user's organization, if known"),
    )
    organization_vat_code = models.CharField(
        _("organization VAT code"),
        max_length=20,
        blank=True,
        help_text=_("VAT code of the user's organization"),
    )
    organization_address = models.CharField(
        _("organization address"),
        max_length=255,
        blank=True,
        help_text=_("Postal address of the user's organization"),
    )

    # Identity assurance (from IdP only)
    eduperson_assurance = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_refeds_assurance_list],
        help_text=_("REFEDS assurance profile URIs from identity provider"),
    )

    query_field = models.CharField(max_length=300, blank=True)
    WHITELIST_FIELDS = [
        "is_superuser",
        "description",
        "username",
        "civil_number",
        "native_name",
        "phone_number",
        "organization",
        "job_title",
        "email",
        "is_staff",
        "is_support",
        "preferred_language",
        "backend_id",
        "is_identity_manager",
        "can_use_personal_access_tokens",
        "affiliations",
        "first_name",
        "last_name",
        # User profile attributes
        "gender",
        "personal_title",
        "place_of_birth",
        "address",
        "country_of_residence",
        "nationality",
        "nationalities",
        "organization_country",
        "organization_type",
        "organization_registry_code",
        "organization_vat_code",
        "organization_address",
        "eduperson_assurance",
        "managed_isds",
        "active_isds",
        "uid_number",
        "primary_gid",
    ]

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @full_name.setter
    def full_name(self, value: str):
        names = value.split()
        self.first_name = " ".join(names[:1])
        self.last_name = " ".join(names[1:])
        self.query_field = normalize_unicode(value)

    tracker = cast(FieldInstanceTracker, FieldTracker())
    objects: UserManager = core_managers.UserActiveManager()
    all_objects = UserManager()
    auth_token: Token | None

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["username"]

    @property
    def should_protect_user_details(self) -> bool:
        """Return True if user profile fields (like organization) must be read-only."""

        protected_methods = django_settings.WALDUR_CORE[
            "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS"
        ]
        return bool(
            self.registration_method and self.registration_method in protected_methods
        )

    def save(self, *args, **kwargs):
        if "update_fields" in kwargs and "query_field" not in kwargs["update_fields"]:
            update_fields = set(kwargs["update_fields"])
            update_fields.add("query_field")
            kwargs["update_fields"] = update_fields
        self.query_field = normalize_unicode(self.full_name)
        super().save(*args, **kwargs)

    def get_log_fields(self):
        return (
            "uuid",
            "full_name",
            "native_name",
            self.USERNAME_FIELD,
            "is_staff",
            "is_support",
            "token_lifetime",
        )

    def get_full_name(self):
        # This method is used in django-reversion as name of revision creator.
        return self.full_name

    def get_short_name(self):
        # This method is used in django-reversion as name of revision creator.
        return self.full_name

    def email_user(self, subject, message, from_email=None):
        """
        Sends an email to this User.
        """
        send_mail(subject, message, [self.email], from_email)

    @classmethod
    def get_permitted_objects(cls, user):
        from waldur_core.structure.filters import filter_visible_users

        queryset = User.objects.all()
        if user.is_staff or user.is_support:
            return queryset
        else:
            return filter_visible_users(queryset, user)

    @transaction.atomic
    def create_request_for_update_email(self, email):
        ChangeEmailRequest.objects.filter(user=self).delete()
        change_request = ChangeEmailRequest.objects.create(
            user=self,
            email=email,
        )
        return change_request

    def __str__(self):
        if self.full_name:
            return f"{self.get_username()} ({self.full_name})"

        return self.get_username()

    @classmethod
    def get_slug_source_field(cls):
        return "username"


class ImpersonatedUser(User):
    """
    Proxy model of User for impersonation functionality.

    Extends User model with impersonator tracking and logging capabilities.
    Used when one user is impersonating another user's session.
    """

    class Meta:
        proxy = True

    impersonator = None

    @property
    def impersonator_uuid(self):
        if self.impersonator:
            return self.impersonator.uuid.hex

    @property
    def impersonator_full_name(self):
        if self.impersonator:
            username = getattr(self.impersonator, self.USERNAME_FIELD)
            if self.impersonator.full_name:
                return username + " / " + self.impersonator.full_name
            return username

    @property
    def impersonator_username(self):
        if self.impersonator:
            return getattr(self.impersonator, self.USERNAME_FIELD)

    def get_log_fields(self):
        log_fields = super().get_log_fields()
        return log_fields + (
            "impersonator_uuid",
            "impersonator_full_name",
            "impersonator_username",
        )

    def __str__(self):
        return super().__str__() + f" impersonator: {self.impersonator}"


class ChangeEmailRequest(UuidMixin, TimeStampedModel):
    """
    Model for handling user email change requests.

    Stores temporary email change requests with UUID identification
    and timestamp tracking. Each user can have only one active
    email change request at a time.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    email = models.EmailField()

    class Meta:
        verbose_name = _("change email request")
        verbose_name_plural = _("change email requests")


def get_ssh_key_fingerprints(ssh_key):
    """
    Calculate SSH key fingerprints in multiple formats.

    Generates MD5, SHA256, and SHA512 fingerprints from an SSH public key.
    MD5 fingerprints are deprecated due to known collisions but maintained
    for compatibility.

    Args:
        ssh_key: SSH public key string

    Returns:
        Tuple of (md5_fingerprint, sha256_fingerprint, sha512_fingerprint)

    References:
        - http://stackoverflow.com/a/6682934/175349
        - http://www.ietf.org/rfc/rfc4716.txt Section 4
    """
    # How to get fingerprint_md5 from ssh key:
    # http://stackoverflow.com/a/6682934/175349
    # http://www.ietf.org/rfc/rfc4716.txt Section 4.
    import base64
    import hashlib

    key_body = base64.b64decode(ssh_key.strip().split()[1].encode("ascii"))
    # calculate legacy md5 - AVOID RELYING ON IT!
    md5_digest = hashlib.md5(key_body).hexdigest()  # noqa: S303
    md5_fp = ":".join(a + b for a, b in zip(md5_digest[::2], md5_digest[1::2]))

    # sha256
    sha256_digest = hashlib.sha256(key_body).digest()
    sha256_b64 = base64.b64encode(sha256_digest).rstrip(b"=")
    sha256_fp = f"SHA256:{sha256_b64.decode('utf-8')}"

    # sha512
    sha512_digest = hashlib.sha512(key_body).digest()
    sha512_b64 = base64.b64encode(sha512_digest).rstrip(b"=")
    sha512_fp = f"SHA512:{sha512_b64.decode('utf-8')}"

    return md5_fp, sha256_fp, sha512_fp


@reversion.register()
class SshPublicKey(TimeStampedModel, LoggableMixin, UuidMixin, models.Model):
    """
    User SSH public key for remote access.

    Stores SSH public keys with automatic fingerprint calculation
    (MD5, SHA256, SHA512) and validation. Used for injection into
    VMs and other resources for secure remote access.

    Automatically calculates and stores multiple fingerprint formats
    when the public key is saved or updated.
    """

    user = models.ForeignKey[User](
        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL, db_index=True
    )
    # Model doesn't inherit NameMixin, because name field can be blank.
    name = models.CharField(max_length=150, blank=True)
    fingerprint_md5 = models.CharField(
        max_length=47
    )  # deprecated due to known collisions
    fingerprint_sha256 = models.CharField(
        max_length=51, blank=True
    )  # len('SHA256:') + 44 chars
    fingerprint_sha512 = models.CharField(
        max_length=94, blank=True
    )  # len('SHA512:') + 88 chars
    public_key = models.TextField(
        validators=[validators.MaxLengthValidator(2000), validate_ssh_public_key]
    )
    is_shared = models.BooleanField(default=False)

    @property
    def type(self) -> str:
        key_parts = self.public_key.split(" ", 1)
        return key_parts[0]

    def get_log_fields(self):
        return (
            "uuid",
            "name",
            "type",
            "fingerprint_md5",
            "fingerprint_sha256",
            "fingerprint_sha512",
        )

    class Meta:
        unique_together = ("user", "name")
        verbose_name = _("SSH public key")
        verbose_name_plural = _("SSH public keys")
        ordering = ["name"]

    def save(
        self, force_insert=False, force_update=False, using=None, update_fields=None
    ):
        # Fingerprint is always set based on public_key
        try:
            md5_fp, sha256_fp, sha512_fp = get_ssh_key_fingerprints(self.public_key)
            self.fingerprint_md5 = md5_fp
            self.fingerprint_sha256 = sha256_fp
            self.fingerprint_sha512 = sha512_fp
        except (IndexError, TypeError):
            logger.exception("Fingerprint calculation has failed")
            raise ValueError(
                _("Public key format is incorrect. Fingerprint calculation has failed.")
            )

        if update_fields and "public_key" in update_fields:
            update_fields.append(
                "fingerprint_md5", "fingerprint_sha256", "fingerprint_sha512"
            )

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def __str__(self):
        return f"{self.name} - {self.fingerprint_sha512}, user: {self.user.username}, {self.user.full_name}"


class RuntimeStateMixin(models.Model):
    """
    Mixin to provide runtime state tracking.

    Adds a runtime_state field with predefined ONLINE/OFFLINE states.
    Used to track the current operational status of resources.
    """

    class RuntimeStates:
        ONLINE = "online"
        OFFLINE = "offline"

    class Meta:
        abstract = True

    runtime_state = models.CharField(_("runtime state"), max_length=150, blank=True)

    @classmethod
    def get_online_state(cls):
        return cls.RuntimeStates.ONLINE

    @classmethod
    def get_offline_state(cls):
        return cls.RuntimeStates.OFFLINE


class StateMixin(ErrorMessageMixin, ConcurrentTransitionMixin):
    """
    Mixin implementing finite state machine (FSM) functionality.

    Provides state management with transitions between creation, updating,
    deletion, OK, and error states. Includes error handling capabilities
    and concurrent transition support.
    """

    class Meta:
        abstract = True

    state = FSMIntegerField(
        default=CoreStates.CREATION_SCHEDULED,
        choices=CoreStates.choices,
    )

    @transition(
        field=state,
        source=[CoreStates.CREATION_SCHEDULED, CoreStates.CREATING],
        target=CoreStates.CREATING,
    )
    def begin_creating(self):
        pass

    @transition(
        field=state,
        source=[CoreStates.UPDATE_SCHEDULED, CoreStates.UPDATING],
        target=CoreStates.UPDATING,
    )
    def begin_updating(self):
        if hasattr(self, "update_triggered"):
            self.update_triggered = django_timezone.now()

    @transition(
        field=state,
        source=[CoreStates.DELETION_SCHEDULED, CoreStates.DELETING],
        target=CoreStates.DELETING,
    )
    def begin_deleting(self):
        pass

    @transition(
        field=state,
        source=[CoreStates.OK, CoreStates.ERRED],
        target=CoreStates.UPDATE_SCHEDULED,
    )
    def schedule_updating(self):
        pass

    @transition(
        field=state,
        source=[CoreStates.OK, CoreStates.ERRED],
        target=CoreStates.DELETION_SCHEDULED,
    )
    def schedule_deleting(self):
        pass

    @transition(field=state, source="*", target=CoreStates.OK)
    def set_ok(self):
        pass

    @transition(field=state, source="*", target=CoreStates.ERRED)
    def set_erred(self):
        pass

    @transition(field=state, source=CoreStates.ERRED, target=CoreStates.OK)
    def recover(self):
        pass

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]


class DescendantMixin:
    """
    Mixin to provide child-parent relationships.

    Each related model can provide list of its parents through the
    get_parents() method. Used for hierarchical data structures
    where objects have parent-child relationships.
    """

    def get_parents(self):
        """Return list instance parents."""
        return []


class BackendModelMixin:
    """
    Mixin for models connected to backend objects.

    Represents models that are synchronized with external backend systems.
    These models cannot be created or updated via admin interface because
    backend queries are not supported in the admin.
    """

    @classmethod
    def get_backend_fields(cls):
        """
        Returns a list of fields that are handled on backend.
        """
        return ()


class BackendMixin(models.Model):
    """
    Mixin to add standard backend_id field.

    Provides a backend_id CharField for storing identifiers from
    external backend systems. Used for mapping local objects to
    their corresponding backend representations.
    """

    class Meta:
        abstract = True

    backend_id = models.CharField(max_length=255, blank=True)


class Feature(models.Model):
    """
    Model for feature flags configuration.

    Stores boolean feature flags with unique keys.
    Used to enable/disable features across the application.
    """

    key = models.TextField(max_length=255, unique=True)
    value = models.BooleanField(default=False)


class NotificationTemplate(UuidMixin, NameMixin, TimeStampedModel):
    """
    Model for storing notification templates.

    Stores template paths for different notification types.
    Used by the notification system to render email and other notifications.
    """

    path = models.CharField(
        _("path"), max_length=150, help_text=_("Example: 'flatpages/default.html'")
    )

    class Meta:
        ordering = ["name", "path"]

    def __str__(self):
        return self.path


class Notification(UuidMixin, DescribableMixin, TimeStampedModel):
    """
    Model for notification configuration.

    Defines notification types with unique keys, enabled/disabled status,
    and associated templates. Used to configure which notifications
    are sent and how they are rendered.
    """

    key = models.CharField(max_length=255, unique=True, blank=False)
    enabled = models.BooleanField(
        default=False, help_text=_("Indicates if notification is enabled or disabled")
    )
    templates = models.ManyToManyField(NotificationTemplate)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key


class ActionMixin(StateMixin):
    """
    Mixin for action tracking with state management.

    Extends StateMixin with action tracking fields including action name,
    action details (JSON), and task ID for background task tracking.
    Used for models that need to track ongoing operations.
    """

    class Meta:
        abstract = True

    action = models.CharField(max_length=50, blank=True)
    action_details = JSONField(default=dict)
    task_id = models.CharField(max_length=155, blank=True, null=True)

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]


@dataclass
class FilterCheckResult:
    """Per-filter outcome for a single rule+user evaluation."""

    name: str
    configured: bool
    matched: bool
    user_value: Any = None
    rule_value: Any = None
    reason: str = ""


@dataclass
class RuleEvaluationResult:
    """Structured outcome of evaluating one rule against one user."""

    matched: bool
    filter_results: list[FilterCheckResult] = field(default_factory=list)


class UserDetailsMatchMixin(models.Model):
    class Meta:
        abstract = True

    user_affiliations = models.JSONField(
        default=list,
        blank=True,
    )
    user_email_patterns = models.JSONField(
        default=list,
        blank=True,
    )
    user_identity_sources = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed identity sources (identity providers).",
    )

    # AAI-based filtering fields
    user_nationalities = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of allowed nationality codes (ISO 3166-1 alpha-2). "
            "User must have one of these."
        ),
    )
    user_organization_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of allowed organization type URNs (SCHAC). User must match one."
        ),
    )
    user_assurance_levels = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of required assurance URIs. User must have ALL of these."),
    )

    @classmethod
    def evaluate_for_user(
        cls, item, user: "User", required: bool = True
    ) -> RuleEvaluationResult:
        """Evaluate a single rule against a user and return a structured breakdown.

        Mirrors the semantics of :meth:`get_objects_by_user_patterns`:

        * basic filters (email patterns, affiliations, identity sources) use OR
          logic — any configured-and-matched filter passes the group;
        * if no basic filter is configured, the group passes by default;
        * AAI filters (nationality OR, organization type OR, assurance level AND)
          are additional requirements — each configured filter must pass;
        * when ``required=False`` and the rule has no filters configured at all,
          the rule matches regardless.
        """
        filter_results: list[FilterCheckResult] = []

        # Basic filters (OR group) — affiliations
        user_affiliations = set(user.affiliations or [])
        rule_affiliations = set(item.user_affiliations or [])
        affiliations_configured = bool(rule_affiliations)
        affiliations_matched = bool(user_affiliations & rule_affiliations)
        filter_results.append(
            FilterCheckResult(
                name="affiliations",
                configured=affiliations_configured,
                matched=affiliations_matched,
                user_value=sorted(user_affiliations) if user_affiliations else [],
                rule_value=sorted(rule_affiliations) if rule_affiliations else [],
                reason=(
                    "Not configured"
                    if not affiliations_configured
                    else (
                        "User affiliation intersects rule"
                        if affiliations_matched
                        else "No user affiliation is listed by the rule"
                    )
                ),
            )
        )

        # Basic filters (OR group) — email patterns
        email_patterns = list(item.user_email_patterns or [])
        email_configured = bool(email_patterns)
        email_matched = email_configured and any(
            cls._is_pattern_match(pattern, user.email) for pattern in email_patterns
        )
        filter_results.append(
            FilterCheckResult(
                name="email_patterns",
                configured=email_configured,
                matched=email_matched,
                user_value=user.email or "",
                rule_value=email_patterns,
                reason=(
                    "Not configured"
                    if not email_configured
                    else (
                        "User email matches a configured pattern"
                        if email_matched
                        else "User email does not match any configured pattern"
                    )
                ),
            )
        )

        # Basic filters (OR group) — identity sources
        identity_sources = list(item.user_identity_sources or [])
        identity_configured = bool(identity_sources)
        identity_matched = (
            identity_configured and user.identity_source in identity_sources
        )
        filter_results.append(
            FilterCheckResult(
                name="identity_sources",
                configured=identity_configured,
                matched=identity_matched,
                user_value=user.identity_source or "",
                rule_value=identity_sources,
                reason=(
                    "Not configured"
                    if not identity_configured
                    else (
                        "User identity source is allowed"
                        if identity_matched
                        else "User identity source is not in the allowed list"
                    )
                ),
            )
        )

        any_basic_configured = (
            affiliations_configured or email_configured or identity_configured
        )
        basic_match = (not any_basic_configured) or (
            affiliations_matched or email_matched or identity_matched
        )

        # AAI filter — nationality (OR group)
        nationalities = list(item.user_nationalities or [])
        nat_configured = bool(nationalities)
        user_nat = getattr(user, "nationality", "") or ""
        user_nats = getattr(user, "nationalities", []) or []
        all_user_nats = ({user_nat} | set(user_nats)) - {""}
        nat_matched = (not nat_configured) or bool(all_user_nats & set(nationalities))
        filter_results.append(
            FilterCheckResult(
                name="nationalities",
                configured=nat_configured,
                matched=nat_matched,
                user_value=sorted(all_user_nats) if all_user_nats else [],
                rule_value=nationalities,
                reason=(
                    "Not configured"
                    if not nat_configured
                    else (
                        "User nationality is in the allowed list"
                        if nat_matched
                        else "User has no nationality in the allowed list"
                    )
                ),
            )
        )

        # AAI filter — organization type (OR group)
        org_types = list(item.user_organization_types or [])
        org_type_configured = bool(org_types)
        user_org_type = getattr(user, "organization_type", "") or ""
        org_type_matched = (not org_type_configured) or (user_org_type in org_types)
        filter_results.append(
            FilterCheckResult(
                name="organization_types",
                configured=org_type_configured,
                matched=org_type_matched,
                user_value=user_org_type,
                rule_value=org_types,
                reason=(
                    "Not configured"
                    if not org_type_configured
                    else (
                        "User organization type is in the allowed list"
                        if org_type_matched
                        else "User organization type is not in the allowed list"
                    )
                ),
            )
        )

        # AAI filter — assurance levels (AND group: user must have ALL required)
        assurance = list(item.user_assurance_levels or [])
        assurance_configured = bool(assurance)
        user_assurance = set(getattr(user, "eduperson_assurance", []) or [])
        required_assurance = set(assurance)
        assurance_matched = (not assurance_configured) or required_assurance.issubset(
            user_assurance
        )
        filter_results.append(
            FilterCheckResult(
                name="assurance_levels",
                configured=assurance_configured,
                matched=assurance_matched,
                user_value=sorted(user_assurance) if user_assurance else [],
                rule_value=assurance,
                reason=(
                    "Not configured"
                    if not assurance_configured
                    else (
                        "User holds all required assurance levels"
                        if assurance_matched
                        else "User is missing one or more required assurance levels"
                    )
                ),
            )
        )

        aai_match = nat_matched and org_type_matched and assurance_matched

        any_configured = any_basic_configured or bool(
            nationalities or org_types or assurance
        )

        if not required and not any_configured:
            return RuleEvaluationResult(matched=True, filter_results=filter_results)

        return RuleEvaluationResult(
            matched=bool(basic_match and aai_match),
            filter_results=filter_results,
        )

    @classmethod
    def get_objects_by_user_patterns(cls, user: "User", required=True):
        return [
            item
            for item in cls.objects.all()
            if cls.evaluate_for_user(item, user, required=required).matched
        ]

    @staticmethod
    def _is_potentially_dangerous_pattern(pattern: str) -> bool:
        """Check if a regex pattern might cause ReDoS."""
        return is_potentially_dangerous_regex(pattern)

    @staticmethod
    def _is_pattern_match(pattern, email):
        """Safely check if email matches pattern, handling invalid regex patterns."""
        if not pattern or not isinstance(pattern, str):
            return False
        if not email or not isinstance(email, str):
            return False

        # Check for potentially dangerous patterns
        if UserDetailsMatchMixin._is_potentially_dangerous_pattern(pattern):
            logger.warning(
                "Potentially dangerous regex pattern rejected: '%s'", pattern[:50]
            )
            return False

        try:
            # Use re.match with a compiled pattern for better performance
            # re.match only matches at the beginning, limiting backtracking
            compiled = re.compile(pattern)
            return bool(compiled.match(email))
        except re.error as e:
            logger.warning("Invalid regex pattern '%s': %s", pattern, e)
            return False

    @staticmethod
    def validate_user_email_patterns(patterns: list) -> None:
        invalid_patterns = []
        dangerous_patterns = []

        for pattern in patterns:
            if not pattern or not isinstance(pattern, str):
                invalid_patterns.append(pattern)
                continue
            try:
                re.compile(pattern)
            except re.error:
                invalid_patterns.append(pattern)
                continue

            # Check for ReDoS patterns
            if UserDetailsMatchMixin._is_potentially_dangerous_pattern(pattern):
                dangerous_patterns.append(pattern)

        errors = []
        if invalid_patterns:
            errors.append(f"Invalid regex patterns: {invalid_patterns}")
        if dangerous_patterns:
            errors.append(
                f"Potentially dangerous patterns (nested quantifiers or too long): {dangerous_patterns}"
            )

        if errors:
            raise serializers.ValidationError(errors)


class AvailableMixin(models.Model):
    class Meta:
        abstract = True

    can_be_managed = models.BooleanField(default=True)


class DailyTableSizeHistory(models.Model):
    """
    Stores daily snapshots of database table sizes for trend analysis.
    Used to detect abnormal growth patterns that may indicate bugs.
    """

    table_name = models.CharField(max_length=150, db_index=True)
    date = models.DateField(db_index=True)
    total_size = models.BigIntegerField(
        help_text="Total size including indexes in bytes"
    )
    data_size = models.BigIntegerField(help_text="Data-only size in bytes")
    row_estimate = models.BigIntegerField(null=True, help_text="Estimated row count")

    class Meta:
        unique_together = ("table_name", "date")
        verbose_name = "Daily table size history"
        verbose_name_plural = "Daily table size history"
        ordering = ["-date", "table_name"]

    def __str__(self):
        return f"{self.table_name} ({self.date})"


class PersonalAccessToken(UuidMixin, NameMixin, TimeStampedModel):
    """Named, scoped, time-limited token for programmatic API access.

    The full token is shown only once at creation. Only the SHA-256 hash
    is stored; lookup is by hash (indexed, unique).
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="personal_access_tokens",
    )
    token_prefix = models.CharField(max_length=10)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(default=list)
    # List of {"content_type_id": int, "object_id": int}.
    # Empty list = no entity restriction (the permission allowlist still applies).
    allowed_scopes = models.JSONField(default=list, blank=True)
    # List of canonical CIDR strings. Empty list = no network restriction.
    allowed_networks = models.JSONField(
        default=list, blank=True, validators=[normalize_network_acl]
    )
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.name} ({self.token_prefix}...)"

    @staticmethod
    def generate_token(expires_at):
        """Return (full_token, prefix, sha256_hex).

        Token format: ``w_<unix_timestamp>_<random>`` so that expiry
        is visible by inspecting the token string.
        """
        ts = int(expires_at.timestamp())
        raw = secrets.token_urlsafe(32)  # 256 bits
        full_token = f"w_{ts}_{raw}"
        prefix = full_token[:8]
        token_hash = hashlib.sha256(full_token.encode()).hexdigest()
        return full_token, prefix, token_hash

    @property
    def is_expired(self):
        return django_timezone.now() >= self.expires_at


class TokenExchangeCode(UuidMixin, TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    # Either token (preferred, references the canonical Token row) or
    # external_token (for OIDC access-token pass-through) carries the secret.
    token = models.ForeignKey(
        "authtoken.Token",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="exchange_codes",
    )
    external_token = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        verbose_name = _("token exchange code")
        verbose_name_plural = _("token exchange codes")

    @classmethod
    def generate_code(cls, user, token=None, external_token=""):
        if token is None and not external_token:
            raise ValueError("token or external_token is required")
        return cls.objects.create(user=user, token=token, external_token=external_token)

    def resolve_token_key(self):
        return self.token.key if self.token_id else self.external_token
