from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from model_utils.models import TimeStampedModel

from waldur_core.core.fields import JSONField
from waldur_core.core.models import UuidMixin

User = get_user_model()


class UserAction(UuidMixin, TimeStampedModel):
    """Individual action items for users"""

    class UrgencyChoices(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="actions")
    action_type = models.CharField(
        max_length=100,
        help_text="Type of action, e.g. 'pending_order', 'expiring_resource'",
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    urgency = models.CharField(max_length=10, choices=UrgencyChoices.choices)

    # Object reference - polymorphic support
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    related_object = GenericForeignKey("content_type", "object_id")

    # Action metadata
    due_date = models.DateTimeField(null=True, blank=True)

    # Navigation context
    route_name = models.CharField(
        max_length=100, blank=True, help_text="UI-Router state name for navigation"
    )
    route_params = JSONField(
        default=dict, blank=True, help_text="Parameters for route navigation"
    )

    # Organizational context
    project_name = models.CharField(max_length=255, blank=True)
    project_uuid = models.UUIDField(null=True, blank=True)
    organization_name = models.CharField(max_length=255, blank=True)
    organization_uuid = models.UUIDField(null=True, blank=True)

    # Offering context
    offering_name = models.CharField(max_length=255, blank=True)
    offering_uuid = models.UUIDField(null=True, blank=True)
    offering_type = models.CharField(max_length=100, blank=True)

    # Resource context
    resource_name = models.CharField(max_length=255, blank=True)
    resource_uuid = models.UUIDField(null=True, blank=True)

    # Order context
    order_type = models.CharField(max_length=100, blank=True)

    # User interaction
    is_silenced = models.BooleanField(default=False)
    silenced_at = models.DateTimeField(null=True, blank=True)
    silenced_until = models.DateTimeField(null=True, blank=True)  # Temporary silence

    # Processing tracking
    last_updated_by_task = models.CharField(max_length=100, blank=True)

    # Corrective actions (stored as JSON)
    corrective_actions = JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "action_type", "object_id", "content_type"],
                name="unique_user_action_per_object",
            )
        ]
        indexes = [
            models.Index(fields=["user", "is_silenced", "urgency"]),
            models.Index(fields=["action_type", "due_date"]),
            models.Index(fields=["created"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.user.username})"

    @property
    def is_temporarily_silenced(self):
        """Check if action is temporarily silenced"""
        if not self.silenced_until:
            return False
        return self.silenced_until > timezone.now()

    @property
    def is_effectively_silenced(self):
        """Check if action is silenced (permanently or temporarily)"""
        return self.is_silenced or self.is_temporarily_silenced

    def get_corrective_actions_for_user(self, user):
        """Get corrective actions filtered by user permissions"""
        from . import providers

        provider = providers.get_provider(self.action_type)
        if not provider:
            return []

        actions = provider.get_corrective_actions(user, self.related_object)

        # Filter by permissions
        return [
            action
            for action in actions
            if provider.can_user_perform_action(user, self.related_object, action)
        ]

    def silence(self, duration_days=None):
        """Silence the action permanently or for a specified duration"""
        self.silenced_at = timezone.now()

        if duration_days:
            from datetime import timedelta

            self.silenced_until = timezone.now() + timedelta(days=duration_days)
        else:
            self.is_silenced = True

        self.save(update_fields=["silenced_at", "silenced_until", "is_silenced"])

    def unsilence(self):
        """Remove silence from the action"""
        self.is_silenced = False
        self.silenced_until = None
        self.save(update_fields=["is_silenced", "silenced_until"])


class UserActionProvider(models.Model):
    """Registry of action providers from different apps"""

    app_name = models.CharField(max_length=100)
    provider_class = models.CharField(max_length=200)
    action_type = models.CharField(max_length=100, unique=True)
    is_enabled = models.BooleanField(default=True)
    schedule = models.CharField(max_length=50, default="0 */6 * * *")  # Cron schedule
    last_execution = models.DateTimeField(null=True, blank=True)
    last_execution_status = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["app_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["app_name", "action_type"],
                name="unique_provider_per_app_action_type",
            )
        ]

    def __str__(self):
        return f"{self.app_name}.{self.action_type}"

    def update_execution_status(self, status, execution_time=None):
        """Update the last execution status"""
        self.last_execution = execution_time or timezone.now()
        self.last_execution_status = status
        self.save(update_fields=["last_execution", "last_execution_status"])


class UserActionExecution(TimeStampedModel):
    """Track when users execute corrective actions"""

    action = models.ForeignKey(
        UserAction, on_delete=models.CASCADE, related_name="executions"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    corrective_action_label = models.CharField(max_length=255)
    executed_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)
    execution_metadata = JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created", "id"]
        indexes = [
            models.Index(fields=["action", "executed_at"]),
            models.Index(fields=["user", "executed_at"]),
        ]

    def __str__(self):
        status = "SUCCESS" if self.success else "FAILED"
        return f"{self.corrective_action_label} - {status} ({self.user.username})"
