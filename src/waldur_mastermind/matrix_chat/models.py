import logging

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django_fsm import FSMField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Customer, Project

logger = logging.getLogger(__name__)


class CryptoLeaseKinds:
    BOOTSTRAP = "bootstrap"
    RESET = "reset"
    # Escrowing a recovery key the user brings from another client.
    IMPORT = "import"
    CHOICES = ((BOOTSTRAP, "Set up"), (RESET, "Reset"), (IMPORT, "Import"))


class MatrixUserProfile(core_models.UuidMixin, TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="matrix_profile",
    )
    matrix_user_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Full Matrix user ID, e.g. @user:domain",
    )
    provisioned = models.BooleanField(
        default=False,
        help_text="True after the user has been provisioned via Admin API",
    )
    provisioned_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_web_session_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When Waldur last signed the user in on a web chat device. "
        "Cleared once none of those devices is left, so the daily prune only "
        "asks the homeserver about users who may still have one.",
    )
    # End-to-end encryption. The recovery key unlocks the user's secret storage
    # (cross-signing keys, key backup, dehydrated device) and is returned only in
    # the user's own web chat session. The lease admits one browser at a time to
    # setting encryption up, or resetting it, so two tabs can't each write secret
    # storage and leave neither recovery key working.
    recovery_key = core_fields.EncryptedTextField(blank=True, default="")
    crypto_lease = models.CharField(max_length=64, blank=True, default="")
    crypto_lease_kind = models.CharField(
        max_length=16, blank=True, default="", choices=CryptoLeaseKinds.CHOICES
    )
    crypto_lease_expires_at = models.DateTimeField(null=True, blank=True)
    # Set while a reset's temporary Matrix password may still work; a periodic
    # sweep replaces any left past this time, so a lost task can't leave it.
    crypto_temporary_password_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Matrix user profile"
        verbose_name_plural = "Matrix user profiles"

    def __str__(self):
        return f"{self.user} -> {self.matrix_user_id}"

    def mark_provisioned(self):
        self.provisioned = True
        self.provisioned_at = timezone.now()
        self.save(update_fields=["provisioned", "provisioned_at"])


# Localpart prefix for project room aliases. The appservice registration
# declares a namespace covering it and tasks.create_room generates addresses
# inside it; both read this so the two cannot drift, which is exactly how the
# namespace ended up not covering the generated aliases.
ROOM_ALIAS_PREFIX = "waldur-"


class RoomStates:
    CREATING = "creating"
    ACTIVE = "active"
    DISABLING = "disabling"
    ARCHIVED = "archived"
    ERROR = "error"

    CHOICES = (
        (CREATING, "Creating"),
        (ACTIVE, "Active"),
        (DISABLING, "Disabling"),
        (ARCHIVED, "Archived"),
        (ERROR, "Error"),
    )


class MatrixRoom(core_models.UuidMixin, TimeStampedModel):
    room_id = models.CharField(
        max_length=255,
        unique=True,
        blank=True,
        # null=True is required alongside unique=True: unprovisioned rooms have
        # no room_id, and Postgres treats multiple NULLs as distinct (multiple
        # empty strings would collide on the unique constraint).
        null=True,
        default=None,
        help_text="Matrix room ID, e.g. !abc:domain",
    )
    room_name = models.CharField(max_length=255, blank=True)
    room_alias = models.CharField(
        max_length=255,
        blank=True,
        help_text="Matrix room alias, e.g. #project-name:domain",
    )
    state = FSMField(
        max_length=20,
        choices=RoomStates.CHOICES,
        default=RoomStates.CREATING,
    )
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Generic relation to scope (Project, Customer, etc.)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
    )
    object_id = models.PositiveIntegerField()
    scope = GenericForeignKey("content_type", "object_id")

    class Meta:
        verbose_name = "Matrix room"
        verbose_name_plural = "Matrix rooms"
        unique_together = ("content_type", "object_id")

    class Permissions:
        customer_path = "project__customer"
        project_path = "project"

    def __str__(self):
        return f"{self.room_name} ({self.room_id})"

    @transition(field=state, source=RoomStates.CREATING, target=RoomStates.ACTIVE)
    def set_active(self):
        pass

    @transition(
        field=state,
        source=[RoomStates.ACTIVE, RoomStates.ERROR],
        target=RoomStates.DISABLING,
    )
    def begin_disabling(self):
        pass

    @transition(field=state, source=RoomStates.DISABLING, target=RoomStates.ARCHIVED)
    def set_archived(self):
        pass

    @transition(field=state, source="*", target=RoomStates.ERROR)
    def set_erred(self):
        pass

    @transition(field=state, source=RoomStates.ERROR, target=RoomStates.CREATING)
    def retry_creating(self):
        self.error_message = ""

    @transition(field=state, source=RoomStates.ARCHIVED, target=RoomStates.ACTIVE)
    def reactivate(self):
        pass

    @transition(field=state, source=RoomStates.ACTIVE, target=RoomStates.CREATING)
    def begin_reprovisioning(self):
        """Reset room for reprovisioning on a new homeserver."""
        self.error_message = ""

    @property
    def project(self):
        from waldur_core.structure.models import Project

        if isinstance(self.scope, Project):
            return self.scope
        return None


class MembershipStates:
    INVITED = "invited"
    JOINED = "joined"
    LEFT = "left"
    BANNED = "banned"
    # No longer in the room.
    GONE = (LEFT, BANNED)

    CHOICES = (
        (INVITED, "Invited"),
        (JOINED, "Joined"),
        (LEFT, "Left"),
        (BANNED, "Banned"),
    )


class MatrixRoomMember(core_models.UuidMixin, TimeStampedModel):
    room = models.ForeignKey(
        MatrixRoom,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
    )
    matrix_user_id = models.CharField(max_length=255)
    power_level = models.IntegerField(default=0)
    membership_state = models.CharField(
        max_length=20,
        choices=MembershipStates.CHOICES,
        default=MembershipStates.INVITED,
    )
    # Joined through the staff Join button rather than through a role. Member
    # sync and role revocation leave such staff and support users in the room
    # while they are active staff or support.
    manually_joined = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Matrix room member"
        verbose_name_plural = "Matrix room members"
        unique_together = ("room", "user")

    def __str__(self):
        return f"{self.matrix_user_id} in {self.room}"


def get_customer_roles_in_project_rooms(customer):
    """Active roles on the customer whose holders belong in its project rooms."""
    # Whoever may create the customer's chat rooms runs its chat, so they sit
    # in every room; other customer roles (support, reader) stay out.
    # A role that gains or loses the permission has its rooms synced by
    # tasks.sync_rooms_of_role.
    return UserRole.objects.filter(
        scope=customer,
        is_active=True,
        role__permissions__permission=PermissionEnum.CREATE_MATRIX_ROOM,
    )


def get_project_rooms(user_roles):
    """Active project rooms the roles are on: a project role's room, and every
    room of a customer role's projects."""
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)
    projects = Project.objects.filter(
        id__in=user_roles.filter(content_type=project_ct).values("object_id")
    ) | Project.objects.filter(
        customer_id__in=user_roles.filter(content_type=customer_ct).values("object_id")
    )
    return MatrixRoom.objects.filter(
        content_type=project_ct,
        object_id__in=projects.values("id"),
        state=RoomStates.ACTIVE,
    )


def has_room_role(user, room):
    """Whether a role puts the user in the room, by the member sync rule."""
    project = room.project
    if not project:
        return UserRole.objects.filter(
            user=user, scope=room.scope, is_active=True
        ).exists()
    return (
        UserRole.objects.filter(user=user, scope=project, is_active=True).exists()
        or get_customer_roles_in_project_rooms(project.customer)
        .filter(user=user)
        .exists()
    )


# A membership that member sync and role revocation leave alone: staff or support
# who joined with the staff Join button and are still staff or support.
STAFF_JOINED = (
    models.Q(manually_joined=True)
    & models.Q(user__is_active=True)
    & (models.Q(user__is_staff=True) | models.Q(user__is_support=True))
)


def keeps_room_access(user, room):
    """Whether the user may stay in the room: an active user whom a role puts
    in it, or staff who joined with the Join button."""
    if not user.is_active:
        return False
    if has_room_role(user, room):
        return True
    return MatrixRoomMember.objects.filter(STAFF_JOINED, room=room, user=user).exists()


class ExportTypes:
    PERIODIC = "periodic"
    ON_DELETION = "on_deletion"
    MANUAL = "manual"

    CHOICES = (
        (PERIODIC, "Periodic"),
        (ON_DELETION, "On deletion"),
        (MANUAL, "Manual"),
    )


class ExportStates:
    PENDING = "pending"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"

    CHOICES = (
        (PENDING, "Pending"),
        (EXPORTING, "Exporting"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    )


class MatrixHistoryExport(core_models.UuidMixin, TimeStampedModel):
    room = models.ForeignKey(
        MatrixRoom,
        on_delete=models.CASCADE,
        related_name="exports",
    )
    export_file = models.FileField(
        upload_to="matrix_exports/%Y/%m/",
        blank=True,
        null=True,
    )
    media_file = models.FileField(
        upload_to="matrix_exports/%Y/%m/media/",
        blank=True,
        null=True,
        help_text="ZIP archive containing downloaded media files",
    )
    media_count = models.IntegerField(default=0)
    export_type = models.CharField(
        max_length=20,
        choices=ExportTypes.CHOICES,
        default=ExportTypes.MANUAL,
    )
    message_count = models.IntegerField(default=0)
    state = models.CharField(
        max_length=20,
        choices=ExportStates.CHOICES,
        default=ExportStates.PENDING,
    )
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Matrix history export"
        verbose_name_plural = "Matrix history exports"
        ordering = ["-created", "id"]

    def __str__(self):
        return f"Export {self.uuid} for {self.room}"


class MatrixAppserviceTransaction(models.Model):
    txn_id = models.CharField(max_length=255, unique=True, db_index=True)
    processed_at = models.DateTimeField(auto_now_add=True)
    event_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-processed_at", "id"]

    def __str__(self):
        return f"Transaction {self.txn_id} ({self.event_count} events)"


class MatrixBotIdentity(TimeStampedModel):
    """The bot's own Matrix device and the secrets only the bot uses.

    The bot's Olm account and sessions live in its crypto store, pickled under
    ``pickle_key``. Without that key the store is useless, and with a wrong one
    the bot refuses to start rather than reset the store. The cross-signing seeds
    let the bot sign its device again after a restart.

    The lease admits one bot process at a time. It is a row and not a Postgres
    advisory lock, which a transaction-pooling PgBouncer would not hold.
    """

    user_id = models.CharField(max_length=255, unique=True)
    device_id = models.CharField(max_length=64)
    pickle_key = core_fields.EncryptedTextField()
    cross_signing_seeds = core_fields.EncryptedTextField(blank=True, default="")
    # Reused across restarts: signing in again would leave one more live token
    # for the device each time, and logging out would delete the device.
    access_token = core_fields.EncryptedTextField(blank=True, default="")
    access_token_homeserver = models.CharField(max_length=255, blank=True, default="")
    lease_holder = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Matrix bot identity"

    def __str__(self):
        return f"{self.user_id} ({self.device_id})"


class OutboxStates:
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"

    CHOICES = ((PENDING, "Pending"), (SENT, "Sent"), (FAILED, "Failed"))


class MatrixOutboxMessage(core_models.UuidMixin, TimeStampedModel):
    """A message for the bot to post, encrypted, into a room.

    Only the bot process holds the keys to send into an encrypted room, so
    everything Waldur posts as the bot is queued here and the bot drains it.
    """

    room = models.ForeignKey(
        MatrixRoom, on_delete=models.CASCADE, related_name="outbox_messages"
    )
    body = models.TextField()
    reply_to = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Event the message replies to; a command is answered once.",
    )
    state = models.CharField(
        max_length=16, choices=OutboxStates.CHOICES, default=OutboxStates.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    error_message = models.TextField(blank=True)
    event_id = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created", "id"]
        indexes = [models.Index(fields=["state", "next_attempt_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["room", "reply_to"],
                condition=~models.Q(reply_to=""),
                name="matrix_outbox_one_reply_per_event",
            )
        ]

    def __str__(self):
        return f"{self.room} [{self.state}]"
