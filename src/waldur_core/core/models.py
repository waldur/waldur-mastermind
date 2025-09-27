import logging
import re
from functools import lru_cache
from typing import cast
from uuid import UUID

from django.apps import apps
from django.conf import settings
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
from waldur_core.core.enums import CoreStates
from waldur_core.core.fields import JSONField, UUIDField
from waldur_core.core.utils import normalize_unicode, send_mail
from waldur_core.core.validators import (
    validate_name,
    validate_phone_number,
    validate_ssh_public_key,
)
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.media.mixins import ImageModelMixin

from .shims import AbstractBaseUser

logger = logging.getLogger(__name__)


DESCRIPTION_LENGTH = 2000

NAME_LENGTH = 150

USERNAME_REGEX = r"^[a-zA-Z0-9_.][a-zA-Z0-9_.-]*[a-zA-Z0-9_.$-]?$"


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
            slug_source = getattr(self, self.get_slug_source_field())
            self.slug = generate_slug(slug_source, self.__class__)

        super().save(*args, **kwargs)

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

    # Find maximum suffix
    max_num = 0
    for slug in existing_slugs:
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
        "affiliations",
        "first_name",
        "last_name",
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

        super().save(force_insert, force_update, using, update_fields)

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
        choices=CoreStates.CHOICES,
    )

    @transition(
        field=state, source=CoreStates.CREATION_SCHEDULED, target=CoreStates.CREATING
    )
    def begin_creating(self):
        pass

    @transition(
        field=state, source=CoreStates.UPDATE_SCHEDULED, target=CoreStates.UPDATING
    )
    def begin_updating(self):
        if hasattr(self, "update_triggered"):
            self.update_triggered = django_timezone.now()

    @transition(
        field=state, source=CoreStates.DELETION_SCHEDULED, target=CoreStates.DELETING
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
        default=True, help_text=_("Indicates if notification is enabled or disabled")
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

    @classmethod
    def get_objects_by_user_patterns(cls, user: User, required=True):
        items = []
        for item in cls.objects.all():
            if (
                not required
                and not item.user_email_patterns
                and not item.user_affiliations
            ):
                items.append(item)

            if set(user.affiliations or []) & set(item.user_affiliations) or any(
                cls._is_pattern_match(pattern, user.email)
                for pattern in item.user_email_patterns
            ):
                items.append(item)

        return items

    def _is_pattern_match(pattern, email):
        """Safely check if email matches pattern, handling invalid regex patterns."""
        if not pattern or not isinstance(pattern, str):
            return False
        try:
            return bool(re.match(pattern, email))
        except re.error as e:
            logger.warning("Invalid regex pattern '%s': %s", pattern, e)
            return False

    @staticmethod
    def validate_user_email_patterns(patterns: list) -> None:
        invalid_patterns = []

        for pattern in patterns:
            if not pattern or not isinstance(pattern, str):
                invalid_patterns.append(pattern)
                continue
            try:
                re.compile(pattern)
            except re.error:
                invalid_patterns.append(pattern)

        if invalid_patterns:
            raise serializers.ValidationError(
                f"Invalid regex patterns: {invalid_patterns}"
            )
