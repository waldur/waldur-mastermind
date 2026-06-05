import logging

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django_fsm import FSMField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models

logger = logging.getLogger(__name__)


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
    # TODO(WAL-9740): encrypt at rest via the field-level encryption
    # infrastructure that ticket builds. Until then the token lives in
    # plaintext alongside the rest of the row.
    access_token = models.CharField(
        max_length=512,
        blank=True,
        help_text="Matrix access token obtained during registration",
    )

    class Meta:
        verbose_name = "Matrix user profile"
        verbose_name_plural = "Matrix user profiles"

    def __str__(self):
        return f"{self.user} -> {self.matrix_user_id}"

    def mark_provisioned(self):
        self.provisioned = True
        self.provisioned_at = timezone.now()
        self.save(update_fields=["provisioned", "provisioned_at"])


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

    class Meta:
        verbose_name = "Matrix room member"
        verbose_name_plural = "Matrix room members"
        unique_together = ("room", "user")

    def __str__(self):
        return f"{self.matrix_user_id} in {self.room}"


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
        ordering = ["-created"]

    def __str__(self):
        return f"Export {self.uuid} for {self.room}"


class MatrixAppserviceTransaction(models.Model):
    txn_id = models.CharField(max_length=255, unique=True, db_index=True)
    processed_at = models.DateTimeField(auto_now_add=True)
    event_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-processed_at"]

    def __str__(self):
        return f"Transaction {self.txn_id} ({self.event_count} events)"
