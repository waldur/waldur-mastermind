import logging
from datetime import datetime, timedelta
from typing import Literal, cast

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker
from rest_framework.exceptions import ValidationError

import waldur_core.media.mixins
from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.core import models as core_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.mixins import PermissionMixin
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import SafeAttributesMixin
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)

from . import managers

logger = logging.getLogger(__name__)


class CallDocument(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.DescribableMixin,
):
    """Documentation files attached to calls for proposals."""

    call_documents: models.Manager["Call"]

    call = models.ForeignKey["Call"]("Call", on_delete=models.CASCADE)
    file = models.FileField(
        upload_to="call_documents",
        blank=True,
        null=True,
        help_text="Documentation for call for proposals.",
    )


class CallManagingOrganisation(
    PermissionMixin,
    core_models.UuidMixin,
    core_models.DescribableMixin,
    waldur_core.media.mixins.ImageModelMixin,
    TimeStampedModel,
):
    """Organizations that create and manage calls for proposals, with one-to-one relationship to Waldur customers."""

    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)

    class Permissions:
        customer_path = "customer"

    class Meta:
        verbose_name = _("Call managing organisation")

    def __str__(self):
        return str(self.customer)

    @property
    def name(self):
        return self.customer.name

    @classmethod
    def get_url_name(cls):
        return "call-managing-organisation"


def filter_calls(user):
    return Q(
        manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
            user
        )
    )


class Call(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    structure_models.StructureLoggableMixin,
    core_models.BackendMixin,
    core_models.SlugMixin,
    PermissionMixin,
):
    """Main entity representing calls for proposals with states (draft, active, archived). Contains configuration for reviewer visibility, review settings, and fixed duration parameters."""

    class States(CallStates):
        pass

    manager = models.ForeignKey(CallManagingOrganisation, on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    state = models.CharField(
        default=States.DRAFT,
        choices=States.CHOICES,
        db_index=True,
        max_length=10,
    )
    offerings = models.ManyToManyField(
        marketplace_models.Offering, through="RequestedOffering"
    )
    documents = models.ManyToManyField(CallDocument, related_name="call_documents")
    external_url = models.URLField(blank=True, null=True)

    reviewer_identity_visible_to_submitters = models.BooleanField(
        default=False,
        help_text="Whether proposal submitters can see reviewer identities. "
        "If False, reviewers appear as 'Reviewer 1', 'Reviewer 2', etc.",
    )
    reviews_visible_to_submitters = models.BooleanField(
        default=True,
        help_text="Whether proposal submitters can see review comments and scores. "
        "If False, submitters only see final approval/rejection status.",
    )

    # Fixed duration that applies to all proposals in this call
    fixed_duration_in_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Fixed duration in days that applies to all proposals in this call",
    )

    # Compliance checklist integration
    compliance_checklist = models.ForeignKey(
        checklist_models.Checklist,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        limit_choices_to={
            "checklist_type": checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        },
        help_text="Compliance checklist that proposals must complete before submission",
    )

    objects = managers.CallManager()
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "manager__customer"
        list_permission = PermissionEnum.LIST_CALLS
        build_query = filter_calls

    def __str__(self):
        return f"{self.name} | {self.manager.customer}"

    @property
    def reviewers(self):
        return get_users(self, RoleEnum.CALL_REVIEWER)

    @property
    def call_managers(self):
        return get_users(self, RoleEnum.CALL_MANAGER)

    @property
    def customer(self):
        return self.manager.customer

    def clean(self):
        """Prevent changing checklist if proposals exist."""
        if self.pk and self.tracker.has_changed("compliance_checklist"):
            if self.proposal_set.exists():
                raise ValidationError(
                    "Cannot change compliance checklist when proposals exist"
                )


def filter_call_proposal_project_role_mappings(user):
    return Q(
        call__manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
            user
        )
    )


class ProposalProjectRoleMapping(
    core_models.UuidMixin,
):
    """Maps proposal roles to project roles in call scope for automatic role assignment upon proposal acceptance."""

    """This model is used to map proposal roles to project roles in call scope."""

    class Permissions:
        customer_path = "call__manager__customer"
        build_query = filter_call_proposal_project_role_mappings

    call = models.ForeignKey(Call, on_delete=models.CASCADE)
    proposal_role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        null=False,
        related_name="proposal_role_mappings",
    )
    project_role = models.ForeignKey(
        Role,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_role_mappings",
    )

    class Meta:
        unique_together = ("call", "proposal_role")

    def clean(self):
        if (
            self.project_role
            and self.project_role.content_type.model_class().__name__ != "Project"
        ):
            raise ValidationError(_("Role should belong to the project type."))
        if self.proposal_role.content_type.model_class().__name__ != "Proposal":
            raise ValidationError(_("Role should belong to the proposal type."))


class RequestedOffering(
    SafeAttributesMixin,
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    """Marketplace offerings available within calls, with approval workflow and state management (requested, accepted, canceled)."""

    class Permissions:
        customer_path = "offering__customer"

    class States(RequestedOfferingStates):
        pass

    approved_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        blank=True,
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    state = models.CharField(
        default=States.REQUESTED,
        choices=States.CHOICES,
        db_index=True,
        max_length=10,
    )
    call = models.ForeignKey(Call, on_delete=models.CASCADE)
    offering = models.ForeignKey(marketplace_models.Offering, on_delete=models.CASCADE)
    plan = models.ForeignKey(
        on_delete=models.CASCADE, to=marketplace_models.Plan, null=True, blank=True
    )


class CallResourceTemplate(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    """Predefined resource templates that proposal creators must use to standardize resource requests across proposals."""

    """Predefined resource templates that proposal creators must use"""

    class Permissions:
        customer_path = "call__manager__customer"

    call = models.ForeignKey(
        Call, on_delete=models.CASCADE, related_name="resource_templates"
    )
    requested_offering = models.ForeignKey(RequestedOffering, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    attributes = models.JSONField(blank=True, default=dict)
    limits = models.JSONField(blank=True, default=dict)
    is_required = models.BooleanField(
        default=False,
        help_text="If True, every proposal must include this resource type",
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )

    class Meta:
        unique_together = ("call", "name")
        verbose_name = _("Call resource template")
        verbose_name_plural = _("Call resource templates")

    def __str__(self):
        return f"{self.call.name} - {self.name}"

    @classmethod
    def get_url_name(cls):
        return "proposal-call-resource-template"


def filter_rounds(user):
    return Q(
        call__manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
            user
        )
    )


class Round(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.SlugMixin,
):
    """Time-bounded submission periods within calls, with configurable review strategies, allocation strategies, and scoring thresholds."""

    class ReviewStrategies:
        AFTER_ROUND = "after_round"
        AFTER_PROPOSAL = "after_proposal"

        CHOICES = (
            (AFTER_ROUND, "After round is closed"),
            (AFTER_PROPOSAL, "After proposal submission"),
        )

    class AllocationStrategies:
        BY_CALL_MANAGER = "by_call_manager"
        AUTOMATIC = "automatic"

        CHOICES = (
            (BY_CALL_MANAGER, "By call manager"),
            (AUTOMATIC, "Automatic based on review scoring"),
        )

    class AllocationTimes:
        ON_DECISION = "on_decision"
        FIXED_DATE = "fixed_date"

        CHOICES = (
            (ON_DECISION, "On decision"),
            (FIXED_DATE, "Fixed date"),
        )

    class Statuses:
        SCHEDULED = "scheduled"
        OPEN = "open"
        ENDED = "ended"

        CHOICES = (
            (SCHEDULED, "Round is scheduled"),
            (OPEN, "Round is open"),
            (ENDED, "Round is ended"),
        )

    review_strategy = models.CharField(
        default=ReviewStrategies.AFTER_ROUND,
        choices=ReviewStrategies.CHOICES,
        db_index=True,
        max_length=15,
    )
    deciding_entity = models.CharField(
        default=AllocationStrategies.AUTOMATIC,
        choices=AllocationStrategies.CHOICES,
        db_index=True,
        max_length=15,
    )
    allocation_time = models.CharField(
        default=AllocationTimes.ON_DECISION,
        choices=AllocationTimes.CHOICES,
        db_index=True,
        max_length=15,
    )
    review_duration_in_days = models.PositiveIntegerField(null=True, blank=True)
    minimum_number_of_reviewers = models.PositiveIntegerField(null=True, blank=True)
    minimal_average_scoring = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    allocation_date = models.DateTimeField(null=True, blank=True)
    start_time = models.DateTimeField()
    cutoff_time = models.DateTimeField()
    call = models.ForeignKey(Call, on_delete=models.PROTECT)
    proposal_set: models.Manager["Proposal"]

    class Permissions:
        customer_path = "call__manager__customer"
        list_permission = PermissionEnum.LIST_ROUNDS
        build_query = filter_rounds

    def __str__(self):
        return f"{self.call.name} | {self.start_time} - {self.cutoff_time}"

    @property
    def name(self) -> str:
        return f"Round {self.start_time.strftime('%d.%m.%Y')}-{self.cutoff_time.strftime('%d.%m.%Y')}"

    @property
    def status(self) -> Literal["scheduled", "open", "ended"]:
        now = timezone.now()

        if self.start_time > now:
            return self.Statuses.SCHEDULED
        elif self.cutoff_time < now:
            return self.Statuses.ENDED
        else:
            return self.Statuses.OPEN

    def save(self, *args, **kwargs):
        if not self.slug:
            # Auto-generate slug based on call manager's organization, call, and round start date
            org_slug = self.call.manager.customer.slug
            call_slug = self.call.slug
            round_date = self.start_time.strftime("%Y%m")
            base_slug = core_models.clean_slug_hyphens(
                f"{org_slug}-{call_slug}-{round_date}"
            ).upper()

            # Ensure uniqueness
            slug = base_slug
            counter = 1
            while Round.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1

            self.slug = slug

        super().save(*args, **kwargs)


class ProposalDocumentation(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Supporting documentation files uploaded with proposals in PDF format."""

    proposal = models.ForeignKey["Proposal"]("Proposal", on_delete=models.CASCADE)
    file = models.FileField(
        upload_to="proposal_project_supporting_documentation",
        blank=True,
        null=True,
        help_text="Upload supporting documentation in PDF format.",
    )


def filter_proposals(user):
    return (
        Q(created_by=user)
        | Q(
            round__call__manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
                user
            )
        )
        | Q(round__call__in=managers.get_connected_calls(user))
    )


class Proposal(
    TimeStampedModel,
    PermissionMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.SlugMixin,
    core_models.DescribableMixin,
    structure_models.StructureLoggableMixin,
    structure_models.ProjectOECDFOS2007CodeMixin,
):
    """Individual proposals submitted to rounds with states (draft, submitted, in_review, accepted, rejected, canceled). Links to Waldur projects and contains detailed project information."""

    class States(ProposalStates):
        pass

    round = models.ForeignKey(Round, on_delete=models.CASCADE)
    state = models.CharField(
        default=States.DRAFT,
        choices=States.CHOICES,
        db_index=True,
        max_length=10,
    )
    project = models.ForeignKey(
        structure_models.Project, on_delete=models.PROTECT, editable=False, null=True
    )
    duration_in_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration in days after provisioning of resources.",
    )
    approved_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        blank=True,
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    project_summary = models.TextField(blank=True)
    project_duration = models.PositiveIntegerField(null=True, blank=True)
    project_is_confidential = models.BooleanField(default=False)
    project_has_civilian_purpose = models.BooleanField(default=False)

    resources = models.ManyToManyField(RequestedOffering, through="RequestedResource")
    allocation_comment = models.CharField(blank=True, max_length=150, null=True)

    # Note: checklist_completions relationship is automatically available via ChecklistCompletion.scope

    tracker = cast(FieldInstanceTracker, FieldTracker())
    requestedresource_set: models.Manager["RequestedResource"]
    review_set: models.Manager["Review"]

    class Permissions:
        customer_path = "round__call__manager__customer"
        build_query = filter_proposals
        list_permission = PermissionEnum.LIST_PROPOSALS

    def __str__(self):
        return f"{self.name} | {self.round.start_time} - {self.round.cutoff_time} | {self.round.call}"

    @classmethod
    def get_url_name(cls):
        return "proposal-proposal"

    class Meta:
        ordering = ["round__start_time"]

    @property
    def checklist_completion(self):
        """Get the checklist completion for this proposal."""
        if not self.round.call.compliance_checklist:
            return None

        try:
            proposal_content_type = ContentType.objects.get_for_model(self)
            return checklist_models.ChecklistCompletion.objects.get(
                scope_content_type=proposal_content_type,
                scope_object_id=self.id,
                checklist=self.round.call.compliance_checklist,
            )
        except checklist_models.ChecklistCompletion.DoesNotExist:
            return None

    def can_submit(self):
        """Check if proposal can be submitted."""
        # Check if call requires compliance checklist
        if self.round.call.compliance_checklist:
            completion = self.checklist_completion
            if not completion:
                return (
                    False,
                    "Compliance checklist completion object missing - please contact support",
                )

            if not completion.is_completed:
                completion_pct = completion.get_completion_percentage()
                unanswered = completion.get_unanswered_required_questions()
                unanswered_count = unanswered.count()
                return (
                    False,
                    f"Compliance checklist must be completed before submission ({completion_pct}% complete, {unanswered_count} required questions remaining)",
                )

        return True, None

    def save(self, *args, **kwargs):
        if not self.slug:
            # Generate slug with Round slug prefix and unique counter
            round_slug = (
                self.round.slug if self.round and self.round.slug else "PROPOSAL"
            )
            # Clean the round slug but keep the separator hyphen
            clean_round_slug = core_models.clean_slug_hyphens(round_slug)
            base_slug = f"{clean_round_slug}-"

            # Find the highest counter for proposals in this round
            existing_proposals = Proposal.objects.filter(
                round=self.round, slug__startswith=base_slug
            ).exclude(pk=self.pk if self.pk else None)

            max_counter = 0
            for proposal in existing_proposals:
                try:
                    # Extract counter from slug (e.g., "ROUND-SLUG-001" -> 1)
                    suffix = proposal.slug[len(base_slug) :]
                    if suffix.isdigit():
                        max_counter = max(max_counter, int(suffix))
                except (ValueError, IndexError):
                    continue

            # Set slug with next available counter (capitalized, 3-digit zero-padded)
            self.slug = f"{base_slug}{max_counter + 1:03d}".upper()

        super().save(*args, **kwargs)

    def submit(self, user):
        """Submit proposal after validation."""
        can_submit, error = self.can_submit()
        if not can_submit:
            raise ValidationError(error)

        self.state = ProposalStates.SUBMITTED
        self.save()


class RequestedResource(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    """Specific resource requests within proposals, linking to marketplace resources with attributes and limits configuration."""

    class Permissions:
        project_path = "proposal__project"

    requested_offering = models.ForeignKey(
        RequestedOffering,
        related_name="+",
        on_delete=models.PROTECT,
    )
    # Optional reference to call resource template
    call_resource_template = models.ForeignKey(
        "CallResourceTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Resource template this request is based on",
    )
    attributes = models.JSONField(blank=True, default=dict)
    limits = models.JSONField(blank=True, default=dict)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    resource = models.ForeignKey(
        marketplace_models.Resource,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
    )
    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE)


class Review(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Peer review system with detailed scoring, public/private comments, and field-specific feedback for all proposal aspects."""

    class States:
        CREATED = "created"
        IN_REVIEW = "in_review"
        SUBMITTED = "submitted"
        REJECTED = "rejected"

        CHOICES = (
            (CREATED, "Created"),
            (IN_REVIEW, "In review"),
            (SUBMITTED, "Submitted"),
            (REJECTED, "Rejected"),
        )

    class Permissions:
        customer_path = "proposal__round__call__manager__customer"

    proposal = models.ForeignKey(Proposal, on_delete=models.PROTECT)
    state = models.CharField(
        default=States.CREATED,
        choices=States.CHOICES,
        db_index=True,
        max_length=10,
    )
    summary_score = models.PositiveSmallIntegerField(blank=True, default=0)
    summary_public_comment = models.TextField(blank=True)
    summary_private_comment = models.TextField(blank=True)
    comment_project_title = models.CharField(max_length=255, null=True, blank=True)
    comment_project_summary = models.CharField(max_length=255, null=True, blank=True)
    comment_project_description = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_duration = models.CharField(max_length=255, null=True, blank=True)
    comment_project_is_confidential = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_has_civilian_purpose = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_supporting_documentation = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_resource_requests = models.CharField(max_length=255, null=True, blank=True)
    comment_team = models.CharField(max_length=255, null=True, blank=True)

    reviewer = models.ForeignKey[core_models.User](
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    @classmethod
    def get_url_name(cls):
        return "proposal-review"

    @property
    def review_end_date(self) -> datetime:
        if not self.proposal.round.review_duration_in_days:
            return

        return self.created + timedelta(
            days=self.proposal.round.review_duration_in_days
        )


class ReviewComment(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Individual comments within reviews for detailed feedback discussion."""

    review = models.ForeignKey(Review, on_delete=models.CASCADE)
    message = models.CharField(max_length=255)


class ResourceAllocator(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
):
    """Entity responsible for allocating resources from calls to specific projects upon proposal acceptance."""

    call = models.ForeignKey(Call, on_delete=models.CASCADE)
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
