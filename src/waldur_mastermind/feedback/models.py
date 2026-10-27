from django.contrib.contenttypes.fields import GenericRelation
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.core import models as core_models


class FeedbackSurvey(core_models.UuidMixin, core_models.NameMixin, TimeStampedModel):
    """A feedback questionnaire shown to its audience after login during its window."""

    intro = models.TextField(
        blank=True, help_text=_("Markdown text shown above the questions.")
    )
    checklist = models.ForeignKey(
        checklist_models.Checklist,
        on_delete=models.PROTECT,
        related_name="feedback_surveys",
        limit_choices_to={"checklist_type": checklist_enums.ChecklistTypes.FEEDBACK},
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    # Same shape as BroadcastMessage.query: all_users, customers and offerings,
    # the latter two as lists of {"uuid", "name"}.
    audience = models.JSONField(default=dict, blank=True)
    max_prompts = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1)],
        help_text=_("How many times a user is prompted at most."),
    )
    snooze_days = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1)],
        help_text=_("Days the prompt stays hidden after it is shown or postponed."),
    )

    class Meta:
        ordering = ["-starts_at", "id"]

    def __str__(self):
        return self.name

    @property
    def is_open(self) -> bool:
        return self.is_active and self.starts_at <= timezone.now() <= self.ends_at


class FeedbackRequest(core_models.UuidMixin, TimeStampedModel):
    """One user's prompt for one survey. It is the scope of the answers."""

    class States:
        PENDING = "pending"
        ANSWERED = "answered"
        DISMISSED = "dismissed"
        # Not answered and can no longer be: the survey ended or the prompts ran out.
        EXPIRED = "expired"

        CHOICES = (
            (PENDING, _("Pending")),
            (ANSWERED, _("Answered")),
            (DISMISSED, _("Dismissed")),
            (EXPIRED, _("Expired")),
        )

    survey = models.ForeignKey(
        FeedbackSurvey, on_delete=models.CASCADE, related_name="requests"
    )
    user = models.ForeignKey(
        core_models.User, on_delete=models.CASCADE, related_name="feedback_requests"
    )
    state = models.CharField(
        max_length=20, choices=States.CHOICES, default=States.PENDING
    )
    prompt_count = models.PositiveSmallIntegerField(default=0)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    # Snapshot taken at creation so answers keep their organisation even if
    # the user's roles change later.
    organization_names = models.JSONField(default=list, blank=True)
    organization_uuids = models.JSONField(default=list, blank=True)
    checklist_completions = GenericRelation(
        checklist_models.ChecklistCompletion,
        content_type_field="scope_content_type",
        object_id_field="scope_object_id",
    )

    class Meta:
        ordering = ["-created", "id"]
        unique_together = ("survey", "user")

    def __str__(self):
        return f"{self.survey} / {self.user}"

    @property
    def checklist_completion(self):
        # all() so a prefetch serves it.
        return next(iter(self.checklist_completions.all()), None)
