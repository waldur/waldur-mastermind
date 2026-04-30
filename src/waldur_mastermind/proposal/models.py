import logging
from datetime import datetime, timedelta
from decimal import Decimal
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
from waldur_mastermind.marketplace.models import (
    SafeAttributesMixin,
    UserAttributeConfigBase,
)
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    AssignmentSources,
    CallStates,
    COIDetectionJobStates,
    COIDetectionJobTypes,
    COIDetectionMethods,
    COISeverityLevels,
    COIStatuses,
    COITypes,
    ExpertiseProficiencyLevels,
    FinancialInterestAmountRanges,
    FinancialInterestEntityTypes,
    FinancialInterestRelationshipTypes,
    MatchingAffinityMethods,
    MatchingAlgorithms,
    ProposalDisclosureLevels,
    ProposalStates,
    PublicationVenueTypes,
    RequestedOfferingStates,
    ReviewerAffiliationTypes,
    ReviewerBidChoices,
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
    RoundStatuses,
    SuggestionSourceTypes,
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
    core_models.UserDetailsMatchMixin,
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
        help_text="Whether proposal applicants can see reviewer identities. "
        "If False, reviewers appear as 'Reviewer 1', 'Reviewer 2', etc.",
    )
    reviews_visible_to_submitters = models.BooleanField(
        default=True,
        help_text="Whether proposal applicants can see review comments and scores. "
        "If False, applicants only see final approval/rejection status.",
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

    # Proposal slug template configuration
    proposal_slug_template = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=(
            "Template for proposal slugs. Supports: {call_slug}, {round_slug}, "
            "{org_slug}, {year}, {month}, {counter}, {counter_padded}. "
            "Default: {round_slug}-{counter_padded}"
        ),
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
        """Prevent changing checklist or slug template if proposals exist."""
        if self.pk and self.proposal_set.exists():
            if self.tracker.has_changed("compliance_checklist"):
                raise ValidationError(
                    "Cannot change compliance checklist when proposals exist"
                )
            if self.tracker.has_changed("proposal_slug_template"):
                raise ValidationError(
                    "Cannot change proposal slug template when proposals exist"
                )


class CallApplicantVisibilityConfig(UserAttributeConfigBase):
    """Configures which applicant fields are visible to reviewers during evaluation."""

    SCOPE_RELATED_NAME = "applicant_visibility_config"
    DEFAULT_CONSTANCE_KEY = "DEFAULT_CALL_USER_ATTRIBUTES"

    call = models.OneToOneField(
        Call,
        on_delete=models.CASCADE,
        related_name="applicant_visibility_config",
    )

    def __str__(self):
        return f"Applicant visibility config for {self.call}"

    @classmethod
    def get_exposed_fields_for_call(cls, call, default_attributes=None) -> list[str]:
        return cls.get_exposed_fields_for_scope(call, default_attributes)


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

    class Statuses(RoundStatuses):
        pass

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
        validators=[MinValueValidator(Decimal("0"))],
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
    science_sub_domain = models.ForeignKey(
        "structure.ScienceSubDomain",
        verbose_name=_("science sub-domain"),
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="proposals",
    )

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
        # Compliance checklists are for evaluation only, not submission blocking
        # Only basic validation - proposals can always be submitted
        return True, None

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_slug()
        super().save(*args, **kwargs)

    def _generate_slug(self):
        """Generate proposal slug using call's template or default pattern."""
        logger = logging.getLogger(__name__)
        template = (
            self.round.call.proposal_slug_template
            if self.round and self.round.call
            else None
        )

        context = self._get_slug_context()

        if template:
            try:
                raw_slug = template.format(**context)
            except (KeyError, ValueError) as e:
                logger.error(
                    f"Failed to format proposal slug template '{template}' "
                    f"for proposal in round {self.round.slug if self.round else 'N/A'}: {e}. "
                    f"Falling back to default format."
                )
                raw_slug = self._get_default_slug(context)
        else:
            raw_slug = self._get_default_slug(context)

        return core_models.clean_slug_hyphens(raw_slug).upper()

    def _get_slug_context(self):
        """Build context dictionary for slug template substitution."""
        now = timezone.now()
        counter = self._calculate_counter()

        call = self.round.call if self.round else None
        round_slug = self.round.slug if self.round and self.round.slug else "PROPOSAL"
        call_slug = call.slug if call and call.slug else ""
        org_slug = call.manager.customer.slug if call and call.manager else ""

        return {
            "call_slug": call_slug,
            "round_slug": round_slug,
            "org_slug": org_slug,
            "year": now.strftime("%Y"),
            "month": now.strftime("%m"),
            "counter": str(counter),
            "counter_padded": f"{counter:03d}",
        }

    def _get_default_slug(self, context):
        """Generate default slug format for backwards compatibility."""
        return f"{context['round_slug']}-{context['counter_padded']}"

    def _calculate_counter(self):
        """Calculate the next counter value for proposals in this round.

        Simply counts existing proposals in the round + 1 for the next counter.
        This approach works regardless of the slug template format.
        """
        existing_count = (
            Proposal.objects.filter(round=self.round)
            .exclude(pk=self.pk if self.pk else None)
            .count()
        )
        return existing_count + 1

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
        IN_REVIEW = "in_review"
        SUBMITTED = "submitted"
        REJECTED = "rejected"

        CHOICES = (
            (IN_REVIEW, "In review"),
            (SUBMITTED, "Submitted"),
            (REJECTED, "Rejected"),
        )

    class Permissions:
        customer_path = "proposal__round__call__manager__customer"

    proposal = models.ForeignKey(Proposal, on_delete=models.PROTECT)
    state = models.CharField(
        default=States.IN_REVIEW,  # Changed from CREATED
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


# =============================================================================
# Reviewer Profile Models
# =============================================================================


def validate_orcid_format(value):
    """Validate ORCID identifier format (0000-0000-0000-0000)."""
    import re

    if value and not re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", value):
        raise ValidationError(
            _("ORCID must be in format 0000-0000-0000-0000 (last character can be X)")
        )


class ReviewerProfile(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.DescribableMixin,
    PermissionMixin,
):
    """
    Extended profile for users acting as reviewers.
    Links to Waldur User account and stores academic/professional information
    for expertise matching and COI detection.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviewer_profile",
    )
    orcid_id = models.CharField(
        max_length=19,
        blank=True,
        null=True,
        unique=True,
        help_text=_("ORCID identifier (format: 0000-0000-0000-0000)"),
        validators=[validate_orcid_format],
    )
    biography = models.TextField(
        blank=True,
        help_text=_("Professional biography / summary"),
    )
    alternative_names = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of name variants used in publications"),
    )

    # ORCID integration fields
    orcid_access_token = models.CharField(max_length=255, blank=True)
    orcid_refresh_token = models.CharField(max_length=255, blank=True)
    orcid_last_sync = models.DateTimeField(null=True, blank=True)

    # Visibility and discovery fields
    is_published = models.BooleanField(
        default=False,
        help_text=_("Whether profile is discoverable by call managers"),
    )
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the profile was published"),
    )
    available_for_reviews = models.BooleanField(
        default=True,
        help_text=_("Whether reviewer is currently accepting review requests"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = None  # Reviewer profiles are user-scoped, not customer-scoped

    class Meta:
        verbose_name = _("Reviewer profile")
        verbose_name_plural = _("Reviewer profiles")

    def __str__(self):
        return f"ReviewerProfile: {self.user.full_name}"

    @classmethod
    def get_url_name(cls):
        return "reviewer-profile"


class ReviewerAffiliation(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Current and historical institutional affiliations for COI detection.
    Can optionally link to Waldur Customer (organization) for enhanced matching.
    """

    reviewer_profile = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="affiliations",
    )
    # Optional link to Waldur organization
    organization = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("Link to Waldur organization if applicable"),
    )
    # For external organizations not in Waldur
    organization_name = models.CharField(
        max_length=255,
        help_text=_("Organization name (used when not linked to Waldur org)"),
    )
    organization_identifier = models.CharField(
        max_length=100,
        blank=True,
        help_text=_("ROR, GRID, or other external identifier"),
    )
    department = models.CharField(max_length=255, blank=True)
    position_title = models.CharField(max_length=255, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Leave empty for current affiliation"),
    )
    is_primary = models.BooleanField(default=False)
    affiliation_type = models.CharField(
        max_length=20,
        choices=ReviewerAffiliationTypes.CHOICES,
        default=ReviewerAffiliationTypes.EMPLOYMENT,
    )

    class Meta:
        verbose_name = _("Reviewer affiliation")
        verbose_name_plural = _("Reviewer affiliations")
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["organization"]),
            models.Index(fields=["organization_name"]),
            models.Index(fields=["end_date"]),
        ]

    def __str__(self):
        org = self.organization.name if self.organization else self.organization_name
        return f"{self.reviewer_profile.user.full_name} @ {org}"

    @property
    def is_current(self):
        return self.end_date is None

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(_("End date cannot be before start date"))


class ExpertiseCategory(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    """
    Hierarchical taxonomy of expertise areas.
    Uses simple self-referential ForeignKey for tree structure.
    """

    code = models.CharField(max_length=20, unique=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    level = models.PositiveSmallIntegerField(
        default=0,
        help_text=_("Depth in hierarchy (0 = root)"),
    )

    class Meta:
        verbose_name = _("Expertise category")
        verbose_name_plural = _("Expertise categories")
        ordering = ["code"]

    def __str__(self):
        return f"{self.code}: {self.name}"

    def save(self, *args, **kwargs):
        # Update level based on parent
        if self.parent:
            self.level = self.parent.level + 1
        else:
            self.level = 0
        super().save(*args, **kwargs)

    @classmethod
    def get_url_name(cls):
        return "expertise-category"


class ReviewerExpertise(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Reviewer's expertise keywords with proficiency levels.
    """

    reviewer_profile = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="expertise_set",
    )
    expertise_keyword = models.CharField(
        max_length=100,
        help_text=_("Free-text keyword"),
    )
    expertise_category = models.ForeignKey(
        ExpertiseCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    proficiency_level = models.CharField(
        max_length=20,
        choices=ExpertiseProficiencyLevels.CHOICES,
        default=ExpertiseProficiencyLevels.FAMILIAR,
    )
    years_experience = models.PositiveIntegerField(null=True, blank=True)
    last_active_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("When last worked in this area"),
    )

    class Meta:
        verbose_name = _("Reviewer expertise")
        verbose_name_plural = _("Reviewer expertise")
        unique_together = ("reviewer_profile", "expertise_keyword")
        indexes = [
            models.Index(fields=["expertise_keyword"]),
            models.Index(fields=["proficiency_level"]),
        ]

    def __str__(self):
        return f"{self.reviewer_profile.user.full_name}: {self.expertise_keyword}"


class ReviewerPublication(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Publications for expertise modeling and co-authorship COI detection.
    """

    reviewer_profile = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="publications",
    )
    title = models.CharField(max_length=500)
    doi = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Digital Object Identifier"),
    )
    publication_year = models.PositiveIntegerField()
    venue = models.CharField(
        max_length=255,
        help_text=_("Journal or conference name"),
    )
    venue_type = models.CharField(
        max_length=20,
        choices=PublicationVenueTypes.CHOICES,
        default=PublicationVenueTypes.JOURNAL,
    )
    abstract = models.TextField(blank=True)
    coauthors = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of co-author names and identifiers"),
    )
    external_ids = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            'External identifiers: {"semantic_scholar": "...", "pubmed": "..."}'
        ),
    )
    is_excluded_from_matching = models.BooleanField(
        default=False,
        help_text=_("User can exclude old papers from expertise matching"),
    )

    class Meta:
        verbose_name = _("Reviewer publication")
        verbose_name_plural = _("Reviewer publications")
        ordering = ["-publication_year"]
        indexes = [
            models.Index(fields=["doi"]),
            models.Index(fields=["publication_year"]),
        ]

    def __str__(self):
        return f"{self.title[:50]}... ({self.publication_year})"


class ReviewerStats(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Review history and performance statistics.
    Updated by signal handlers and periodic tasks.
    """

    reviewer_profile = models.OneToOneField(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="stats",
    )
    total_reviews_completed = models.PositiveIntegerField(default=0)
    total_reviews_declined = models.PositiveIntegerField(default=0)
    total_reviews_timeout = models.PositiveIntegerField(default=0)
    average_review_time_days = models.FloatField(null=True, blank=True)
    average_score_given = models.FloatField(null=True, blank=True)
    last_review_date = models.DateField(null=True, blank=True)

    # Quality metrics (updated by call managers)
    quality_rating = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1.0)],
    )
    quality_rating_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("Reviewer statistics")
        verbose_name_plural = _("Reviewer statistics")

    def __str__(self):
        return f"Stats for {self.reviewer_profile.user.full_name}"


# =============================================================================
# Conflict of Interest Models
# =============================================================================


class CallCOIConfiguration(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Per-call COI detection settings and thresholds.
    """

    call = models.OneToOneField(
        Call,
        on_delete=models.CASCADE,
        related_name="coi_configuration",
    )

    # Automated detection settings
    coauthorship_lookback_years = models.PositiveIntegerField(
        default=3,
        help_text=_("Years to look back for co-authorship detection"),
    )
    coauthorship_threshold_papers = models.PositiveIntegerField(
        default=1,
        help_text=_("Minimum shared papers to trigger COI"),
    )
    institutional_lookback_years = models.PositiveIntegerField(
        default=2,
        help_text=_("Years to look back for former institution detection"),
    )
    include_same_department = models.BooleanField(
        default=True,
        help_text=_("Detect same-department as COI"),
    )
    include_same_institution = models.BooleanField(
        default=True,
        help_text=_("Detect same-institution as COI"),
    )

    # COI type classification
    recusal_required_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_("COI types requiring automatic recusal"),
    )
    management_allowed_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_("COI types allowing management plan"),
    )
    disclosure_only_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_("COI types requiring disclosure only"),
    )

    # Automated detection toggles
    auto_detect_coauthorship = models.BooleanField(
        default=True,
        help_text=_("Enable automated co-authorship detection"),
    )
    auto_detect_institutional = models.BooleanField(
        default=True,
        help_text=_("Enable automated institutional affiliation detection"),
    )
    auto_detect_named_personnel = models.BooleanField(
        default=True,
        help_text=_("Enable detection of reviewer named in proposals"),
    )

    # Invitation disclosure settings
    invitation_proposal_disclosure = models.CharField(
        max_length=30,
        choices=ProposalDisclosureLevels.CHOICES,
        default=ProposalDisclosureLevels.TITLES_ONLY,
        help_text=_("Level of proposal information disclosed in reviewer invitations"),
    )

    class Meta:
        verbose_name = _("Call COI configuration")
        verbose_name_plural = _("Call COI configurations")

    def __str__(self):
        return f"COI Config for {self.call.name}"

    @classmethod
    def get_url_name(cls):
        return "call-coi-configuration"


def filter_conflicts_of_interest(user):
    """Filter COI records based on user's roles."""
    return (
        Q(reviewer__user=user)  # Own COIs
        | Q(
            call__manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
                user
            )
        )  # Call manager COIs
    )


class ConflictOfInterest(
    TimeStampedModel,
    core_models.UuidMixin,
    structure_models.StructureLoggableMixin,
    PermissionMixin,
):
    """
    Individual COI instance between reviewer and proposal/applicant.
    Supports audit logging for compliance.
    """

    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="conflicts",
    )
    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="conflicts",
    )
    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="conflicts",
    )

    # Who/what is the conflict with
    conflicting_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("Specific person causing conflict"),
    )
    conflicting_organization = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Conflict details
    coi_type = models.CharField(
        max_length=20,
        choices=COITypes.CHOICES,
        db_index=True,
    )
    severity = models.CharField(
        max_length=20,
        choices=COISeverityLevels.CHOICES,
        db_index=True,
    )

    # Detection source
    detection_method = models.CharField(
        max_length=20,
        choices=COIDetectionMethods.CHOICES,
    )
    detected_at = models.DateTimeField(auto_now_add=True)

    # Evidence
    evidence_description = models.TextField()
    evidence_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            'Structured evidence: {"papers": [...], "affiliation_overlap": {...}}'
        ),
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=COIStatuses.CHOICES,
        default=COIStatuses.PENDING,
        db_index=True,
    )

    # Management
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="coi_reviews_performed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)
    management_plan = models.TextField(
        blank=True,
        help_text=_("If waived, how is it managed"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "call__manager__customer"
        build_query = filter_conflicts_of_interest

    class Meta:
        verbose_name = _("Conflict of interest")
        verbose_name_plural = _("Conflicts of interest")
        ordering = ["-detected_at"]
        indexes = [
            models.Index(fields=["call", "status"]),
            models.Index(fields=["reviewer", "proposal"]),
            models.Index(fields=["coi_type", "severity"]),
        ]

    def __str__(self):
        return f"COI: {self.reviewer.user.full_name} - {self.get_coi_type_display()}"

    @classmethod
    def get_url_name(cls):
        return "conflict-of-interest"

    def get_log_fields(self):
        return (
            "uuid",
            "coi_type",
            "severity",
            "status",
            "detection_method",
        )


class COIDisclosureForm(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Periodic COI disclosure submitted by reviewer.
    Captures general disclosures or call-specific disclosures.
    """

    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="disclosure_forms",
    )
    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="disclosure_forms",
        help_text=_("Null for general annual disclosure"),
    )

    # Certification
    certified = models.BooleanField(default=False)
    certification_date = models.DateTimeField(null=True, blank=True)
    certification_statement = models.TextField(
        blank=True,
        help_text=_("Legal text they agreed to"),
    )

    # Disclosure content
    has_financial_interests = models.BooleanField(default=False)
    has_personal_relationships = models.BooleanField(default=False)
    personal_relationships = models.JSONField(
        default=list,
        blank=True,
    )
    has_other_conflicts = models.BooleanField(default=False)
    other_conflicts_description = models.TextField(blank=True)

    # Validity
    valid_until = models.DateField(
        help_text=_("Typically 1 year from certification"),
    )
    is_current = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("COI disclosure form")
        verbose_name_plural = _("COI disclosure forms")
        ordering = ["-certification_date"]
        indexes = [
            models.Index(fields=["reviewer", "call"]),
            models.Index(fields=["is_current", "valid_until"]),
        ]

    def __str__(self):
        scope = self.call.name if self.call else "General"
        return f"Disclosure: {self.reviewer.user.full_name} ({scope})"

    @classmethod
    def get_url_name(cls):
        return "coi-disclosure"


class COIDisclosureFinancialInterest(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Detailed financial interest disclosure.
    """

    disclosure_form = models.ForeignKey(
        COIDisclosureForm,
        on_delete=models.CASCADE,
        related_name="financial_interests",
    )
    entity_name = models.CharField(max_length=255)
    entity_type = models.CharField(
        max_length=20,
        choices=FinancialInterestEntityTypes.CHOICES,
    )
    relationship_type = models.CharField(
        max_length=20,
        choices=FinancialInterestRelationshipTypes.CHOICES,
    )
    amount_range = models.CharField(
        max_length=20,
        choices=FinancialInterestAmountRanges.CHOICES,
    )
    is_ongoing = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = _("Financial interest disclosure")
        verbose_name_plural = _("Financial interest disclosures")

    def __str__(self):
        return f"{self.entity_name} ({self.get_relationship_type_display()})"


# =============================================================================
# Reviewer Pool Models
# =============================================================================


def filter_call_reviewer_pool(user):
    """Filter reviewer pool records based on user's roles."""
    return (
        Q(reviewer__user=user)  # Own pool memberships
        | Q(
            call__manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
                user
            )
        )  # Call manager access
    )


class CallReviewerPool(
    TimeStampedModel,
    core_models.UuidMixin,
    PermissionMixin,
):
    """
    Invitation and membership of reviewers for a specific call.
    """

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="reviewer_pool",
    )
    # Nullable to support email invitations where profile doesn't exist yet
    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pool_memberships",
    )
    # Email invitation fields
    invited_email = models.EmailField(
        blank=True,
        help_text=_("Email address for direct invitations"),
    )
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewer_invitations",
        help_text=_("Waldur user if email matches existing account"),
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    invited_at = models.DateTimeField(auto_now_add=True)
    invitation_status = models.CharField(
        max_length=20,
        choices=ReviewerPoolInvitationStatuses.CHOICES,
        default=ReviewerPoolInvitationStatuses.PENDING,
        db_index=True,
    )
    response_date = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True)

    # Assignment limits
    max_assignments = models.PositiveIntegerField(default=5)
    current_assignments = models.PositiveIntegerField(default=0)

    # Matching score
    expertise_match_score = models.FloatField(
        null=True,
        blank=True,
        help_text=_("Calculated affinity to call topics (0-1)"),
    )

    # Invitation token for response without login
    invitation_token = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
    )
    invitation_expires_at = models.DateTimeField(null=True, blank=True)

    # Manager override fields
    override_reason = models.TextField(
        blank=True,
        help_text=_("Reason for manager override of invitation status."),
    )
    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    overridden_at = models.DateTimeField(null=True, blank=True)

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "call__manager__customer"
        build_query = filter_call_reviewer_pool

    class Meta:
        verbose_name = _("Call reviewer pool member")
        verbose_name_plural = _("Call reviewer pool members")
        ordering = ["-invited_at"]
        indexes = [
            models.Index(fields=["invitation_status"]),
            models.Index(fields=["invitation_token"]),
        ]
        constraints = [
            # Unique constraint for reviewer-based invitations
            models.UniqueConstraint(
                fields=["call", "reviewer"],
                condition=models.Q(reviewer__isnull=False),
                name="unique_call_reviewer",
            ),
            # Unique constraint for email-based invitations
            models.UniqueConstraint(
                fields=["call", "invited_email"],
                condition=models.Q(invited_email__gt=""),
                name="unique_call_invited_email",
            ),
            # Ensure either reviewer or invited_email is provided
            models.CheckConstraint(
                condition=models.Q(reviewer__isnull=False)
                | models.Q(invited_email__gt=""),
                name="reviewer_or_email_required",
            ),
        ]

    def __str__(self):
        if self.reviewer:
            return f"{self.reviewer.user.full_name} - {self.call.name}"
        return f"{self.invited_email} - {self.call.name}"

    @classmethod
    def get_url_name(cls):
        return "call-reviewer-pool"

    def save(self, *args, **kwargs):
        if not self.invitation_token:
            import secrets

            self.invitation_token = secrets.token_urlsafe(48)
        super().save(*args, **kwargs)


class ReviewerSuggestion(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Algorithm-generated reviewer suggestions for call managers.
    Created when running the affinity algorithm on published profiles.
    """

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="reviewer_suggestions",
    )
    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="suggestions",
    )

    # Affinity scores
    affinity_score = models.FloatField(
        help_text=_("Combined affinity score (0-1)"),
    )
    keyword_score = models.FloatField(
        null=True,
        blank=True,
        help_text=_("Keyword matching score"),
    )
    text_score = models.FloatField(
        null=True,
        blank=True,
        help_text=_("TF-IDF text similarity score"),
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=ReviewerSuggestionStatuses.CHOICES,
        default=ReviewerSuggestionStatuses.PENDING,
        db_index=True,
    )

    # Manager decision
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    # Justification data (stored for fast retrieval)
    matched_keywords = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Keywords from reviewer's expertise that matched the source text"),
    )
    top_matching_proposals = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "Top proposals with highest affinity: [{uuid, name, slug, affinity}, ...]"
        ),
    )

    # Track what source was used for this suggestion
    source_type = models.CharField(
        max_length=30,
        choices=SuggestionSourceTypes.CHOICES,
        default=SuggestionSourceTypes.ALL_PROPOSALS,
        help_text=_("What content was used to generate this suggestion"),
    )

    class Meta:
        verbose_name = _("Reviewer suggestion")
        verbose_name_plural = _("Reviewer suggestions")
        unique_together = ("call", "reviewer")
        ordering = ["-affinity_score"]
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Suggestion: {self.reviewer.user.full_name} for {self.call.name}"

    @classmethod
    def get_url_name(cls):
        return "reviewer-suggestion"


class COIDetectionJob(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Track progress of COI detection batch jobs."""

    call = models.ForeignKey(Call, on_delete=models.CASCADE, related_name="coi_jobs")
    job_type = models.CharField(
        max_length=20,
        choices=COIDetectionJobTypes.CHOICES,
    )
    state = models.CharField(
        max_length=20,
        choices=COIDetectionJobStates.CHOICES,
        default=COIDetectionJobStates.PENDING,
    )

    # Progress tracking
    total_pairs = models.PositiveIntegerField(default=0)
    processed_pairs = models.PositiveIntegerField(default=0)
    conflicts_found = models.PositiveIntegerField(default=0)

    # Celery integration
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    # Results
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    # Configuration snapshot (for reproducibility/audit)
    config_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("COI detection job")
        verbose_name_plural = _("COI detection jobs")
        ordering = ["-created"]

    def __str__(self):
        return f"COI Job {self.uuid} - {self.call.name} ({self.state})"

    @property
    def progress_percentage(self):
        if self.total_pairs == 0:
            return 0
        return round((self.processed_pairs / self.total_pairs) * 100, 1)


# =============================================================================
# Matching Models
# =============================================================================


class MatchingConfiguration(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Algorithm settings for reviewer-proposal matching.
    """

    call = models.OneToOneField(
        Call,
        on_delete=models.CASCADE,
        related_name="matching_configuration",
    )

    # Affinity computation
    affinity_method = models.CharField(
        max_length=20,
        choices=MatchingAffinityMethods.CHOICES,
        default=MatchingAffinityMethods.COMBINED,
    )
    keyword_weight = models.FloatField(
        default=0.4,
        validators=[MinValueValidator(0.0)],
    )
    text_weight = models.FloatField(
        default=0.6,
        validators=[MinValueValidator(0.0)],
    )

    # Assignment constraints
    min_reviewers_per_proposal = models.PositiveIntegerField(default=3)
    max_reviewers_per_proposal = models.PositiveIntegerField(default=5)
    min_proposals_per_reviewer = models.PositiveIntegerField(default=3)
    max_proposals_per_reviewer = models.PositiveIntegerField(default=10)

    # Algorithm selection
    algorithm = models.CharField(
        max_length=20,
        choices=MatchingAlgorithms.CHOICES,
        default=MatchingAlgorithms.MINMAX,
    )
    min_affinity_threshold = models.FloatField(
        default=0.1,
        validators=[MinValueValidator(0.0)],
        help_text=_("Minimum affinity score for FairFlow algorithm"),
    )

    # Reviewer preferences (bids)
    use_reviewer_bids = models.BooleanField(default=True)
    bid_weight = models.FloatField(
        default=0.3,
        validators=[MinValueValidator(0.0)],
    )

    class Meta:
        verbose_name = _("Matching configuration")
        verbose_name_plural = _("Matching configurations")

    def __str__(self):
        return f"Matching Config for {self.call.name}"

    @classmethod
    def get_url_name(cls):
        return "matching-configuration"

    def clean(self):
        if abs(self.keyword_weight + self.text_weight - 1.0) > 0.001:
            raise ValidationError(_("Keyword weight and text weight must sum to 1.0"))


class ReviewerProposalAffinity(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Cached affinity scores between reviewers and proposals.
    """

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="affinities",
    )
    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="affinities",
    )
    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        related_name="affinities",
    )

    # Computed scores
    affinity_score = models.FloatField(
        help_text=_("Combined affinity score (0-1)"),
    )
    keyword_score = models.FloatField(
        null=True,
        blank=True,
        help_text=_("Keyword-based affinity score"),
    )
    text_score = models.FloatField(
        null=True,
        blank=True,
        help_text=_("Text-based (TF-IDF) affinity score"),
    )

    class Meta:
        verbose_name = _("Reviewer proposal affinity")
        verbose_name_plural = _("Reviewer proposal affinities")
        unique_together = ("call", "reviewer", "proposal")
        indexes = [
            models.Index(fields=["call", "affinity_score"]),
        ]

    def __str__(self):
        return f"{self.reviewer.user.full_name} - {self.proposal.name}: {self.affinity_score:.2f}"


class ProposedAssignment(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Proposed reviewer assignments from matching algorithm.
    """

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="proposed_assignments",
    )
    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="proposed_assignments",
    )
    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        related_name="proposed_assignments",
    )

    affinity_score = models.FloatField()
    algorithm_used = models.CharField(
        max_length=20,
        choices=MatchingAlgorithms.CHOICES,
    )
    rank = models.PositiveIntegerField(
        default=0,
        help_text=_("Assignment rank (1 = best match)"),
    )

    # Deployment status
    is_deployed = models.BooleanField(default=False)
    deployed_at = models.DateTimeField(null=True, blank=True)
    deployed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("Proposed assignment")
        verbose_name_plural = _("Proposed assignments")
        unique_together = ("call", "reviewer", "proposal")
        ordering = ["proposal", "rank"]

    def __str__(self):
        return (
            f"{self.reviewer.user.full_name} -> {self.proposal.name} (rank {self.rank})"
        )


class ReviewerBid(
    core_models.UuidMixin,
    core_models.TimeStampedModel,
):
    """
    Reviewer bids indicating their preference/availability for reviewing proposals.
    """

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="reviewer_bids",
    )
    reviewer = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="bids",
    )
    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        related_name="reviewer_bids",
    )

    bid = models.CharField(
        max_length=20,
        choices=ReviewerBidChoices.CHOICES,
        help_text=_("Reviewer's preference for reviewing this proposal"),
    )

    comment = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional comment explaining the bid"),
    )

    # Metadata
    submitted_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Reviewer bid")
        verbose_name_plural = _("Reviewer bids")
        unique_together = ("call", "reviewer", "proposal")
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.reviewer.user.full_name} - {self.proposal.name}: {self.bid}"

    @property
    def bid_weight(self) -> float:
        """Get the weight for this bid for matching algorithm."""
        return ReviewerBidChoices.WEIGHTS.get(self.bid, 0.0)


# =============================================================================
# Assignment Models (Stage 2 - Proposal Assignment Workflow)
# =============================================================================


class CallAssignmentConfiguration(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """
    Per-call assignment settings for the two-stage reviewer workflow.
    Controls how proposal assignments are managed after pool construction.
    """

    call = models.OneToOneField(
        Call,
        on_delete=models.CASCADE,
        related_name="assignment_configuration",
    )

    # Reassignment settings
    auto_reassign_on_decline = models.BooleanField(
        default=False,
        help_text=_(
            "Automatically assign next-best reviewer when someone declines. "
            "If False, manager must manually approve reassignments."
        ),
    )
    max_auto_reassign_attempts = models.PositiveIntegerField(
        default=3,
        help_text=_(
            "Maximum automatic reassignment attempts before requiring manual intervention."
        ),
    )

    # Expiration settings
    assignment_expiration_days = models.PositiveIntegerField(
        default=7,
        help_text=_("Days until assignment invitation expires if not responded to."),
    )

    # Notification settings
    send_reminder_before_expiry_days = models.PositiveIntegerField(
        default=2,
        help_text=_("Days before expiry to send reminder notification."),
    )

    class Meta:
        verbose_name = _("Call assignment configuration")
        verbose_name_plural = _("Call assignment configurations")

    def __str__(self):
        return f"Assignment Config for {self.call.name}"

    @classmethod
    def get_url_name(cls):
        return "call-assignment-configuration"


def filter_assignment_batches(user):
    """Filter assignment batches based on user's roles."""
    return (
        Q(reviewer_pool_entry__reviewer__user=user)  # Reviewer's own batches
        | Q(call__in=managers.get_connected_calls(user, RoleEnum.CALL_MANAGER))
    )


class AssignmentBatch(
    TimeStampedModel,
    core_models.UuidMixin,
    PermissionMixin,
):
    """
    A batch of proposal assignments sent to a single reviewer.
    Represents the Stage 2 invitation in the two-stage reviewer workflow.

    Flow:
    1. Call manager generates assignments (status: draft)
    2. Manager reviews and sends batch (status: sent)
    3. Reviewer responds to individual items (status: responded when all items handled)
    4. If not responded in time (status: expired)
    """

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="assignment_batches",
    )
    reviewer_pool_entry = models.ForeignKey(
        "CallReviewerPool",
        on_delete=models.CASCADE,
        related_name="assignment_batches",
    )

    # Batch status
    status = models.CharField(
        max_length=20,
        choices=AssignmentBatchStatuses.CHOICES,
        default=AssignmentBatchStatuses.DRAFT,
        db_index=True,
    )

    # Timing
    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the invitation was sent to the reviewer."),
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the invitation expires."),
    )
    responded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the reviewer completed their response."),
    )

    # Source and audit
    source = models.CharField(
        max_length=20,
        choices=AssignmentSources.CHOICES,
        default=AssignmentSources.ALGORITHM,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("User who created/approved this batch."),
    )

    # Invitation token for response via email link
    invitation_token = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
    )

    # Notes from manager
    manager_notes = models.TextField(
        blank=True,
        help_text=_("Optional notes from call manager to reviewer."),
    )

    # Notification tracking
    reminder_sent = models.BooleanField(
        default=False,
        help_text=_("Whether expiry reminder has been sent to reviewer."),
    )
    manager_notified = models.BooleanField(
        default=False,
        help_text=_("Whether manager has been notified of expiration."),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "call__manager__customer"
        build_query = filter_assignment_batches

    class Meta:
        verbose_name = _("Assignment batch")
        verbose_name_plural = _("Assignment batches")
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["invitation_token"]),
        ]

    def __str__(self):
        reviewer_name = (
            self.reviewer_pool_entry.reviewer.user.full_name
            if self.reviewer_pool_entry.reviewer
            else self.reviewer_pool_entry.invited_email
        )
        return f"Assignment Batch: {reviewer_name} - {self.call.name}"

    @classmethod
    def get_url_name(cls):
        return "assignment-batch"

    def save(self, *args, **kwargs):
        if not self.invitation_token:
            import secrets

            self.invitation_token = secrets.token_urlsafe(48)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        """Check if the batch invitation has expired."""
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    @property
    def items_pending_count(self) -> int:
        """Count of items still awaiting response."""
        return self.items.filter(status=AssignmentItemStatuses.PENDING).count()

    @property
    def items_accepted_count(self) -> int:
        """Count of accepted items."""
        return self.items.filter(status=AssignmentItemStatuses.ACCEPTED).count()

    @property
    def items_declined_count(self) -> int:
        """Count of declined items."""
        return self.items.filter(status=AssignmentItemStatuses.DECLINED).count()

    def send_invitation(self, user=None):
        """
        Send the assignment batch invitation to the reviewer.
        Sets status to SENT and calculates expiration date.
        """
        if self.status != AssignmentBatchStatuses.DRAFT:
            raise ValidationError(_("Can only send draft batches."))

        # Get expiration days from call config or use default
        try:
            config = self.call.assignment_configuration
            expiration_days = config.assignment_expiration_days
        except CallAssignmentConfiguration.DoesNotExist:
            expiration_days = 7

        self.status = AssignmentBatchStatuses.SENT
        self.sent_at = timezone.now()
        self.expires_at = timezone.now() + timedelta(days=expiration_days)
        self.save(update_fields=["status", "sent_at", "expires_at"])

        # TODO: Send email notification to reviewer


def filter_assignment_items(user):
    """Filter assignment items based on user's roles."""
    return (
        Q(batch__reviewer_pool_entry__reviewer__user=user)  # Reviewer's own items
        | Q(batch__call__in=managers.get_connected_calls(user, RoleEnum.CALL_MANAGER))
    )


class AssignmentItem(
    TimeStampedModel,
    core_models.UuidMixin,
    PermissionMixin,
):
    """
    Individual proposal assignment within an AssignmentBatch.
    Each item represents one proposal assigned to the reviewer.

    The reviewer can accept or decline each item independently.
    Accepting an item creates a Review record.
    """

    batch = models.ForeignKey(
        AssignmentBatch,
        on_delete=models.CASCADE,
        related_name="items",
    )
    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        related_name="assignment_items",
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=AssignmentItemStatuses.CHOICES,
        default=AssignmentItemStatuses.PENDING,
        db_index=True,
    )

    # Affinity score (why this reviewer was selected)
    affinity_score = models.FloatField(
        null=True,
        blank=True,
        help_text=_("Affinity score used for assignment (0-1)."),
    )

    # COI pre-check results
    has_coi = models.BooleanField(
        default=False,
        help_text=_("Whether COI was detected during pre-check."),
    )
    coi_records = models.ManyToManyField(
        "ConflictOfInterest",
        blank=True,
        related_name="blocked_assignments",
        help_text=_("COI records that block this assignment."),
    )

    # Response tracking
    responded_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    decline_reason = models.TextField(
        blank=True,
        help_text=_("Reason provided by reviewer for declining."),
    )

    # Link to created Review (set when accepted)
    review = models.OneToOneField(
        Review,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignment_item",
        help_text=_("The Review record created when this assignment was accepted."),
    )

    # Reassignment tracking
    reassigned_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reassigned_to_items",
        help_text=_("If this is a reassignment, the original item."),
    )
    reassign_count = models.PositiveIntegerField(
        default=0,
        help_text=_("Number of times this proposal has been reassigned."),
    )

    # Manager override fields
    override_reason = models.TextField(
        blank=True,
        help_text=_("Reason for manager override of COI block."),
    )
    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    overridden_at = models.DateTimeField(null=True, blank=True)

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "batch__call__manager__customer"
        build_query = filter_assignment_items

    class Meta:
        verbose_name = _("Assignment item")
        verbose_name_plural = _("Assignment items")
        unique_together = ("batch", "proposal")
        ordering = ["-affinity_score"]
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Assignment: {self.proposal.name} -> {self.batch}"

    @classmethod
    def get_url_name(cls):
        return "assignment-item"

    def accept(self, user=None):
        """
        Accept this assignment and create a Review record.
        """
        if self.status != AssignmentItemStatuses.PENDING:
            raise ValidationError(_("Can only accept pending assignments."))

        if self.has_coi:
            raise ValidationError(
                _(
                    "Cannot accept assignment with unresolved COI. Please contact the call manager."
                )
            )

        # Create the Review record
        reviewer_profile = self.batch.reviewer_pool_entry.reviewer
        if not reviewer_profile:
            raise ValidationError(_("Reviewer profile not found."))

        # Create review in IN_REVIEW state so reviewer can immediately start working
        review = Review.objects.create(
            proposal=self.proposal,
            reviewer=reviewer_profile.user,
            state=Review.States.IN_REVIEW,
        )

        self.status = AssignmentItemStatuses.ACCEPTED
        self.responded_at = timezone.now()
        self.review = review
        self.save(update_fields=["status", "responded_at", "review"])

        # Update batch status if all items are handled
        self._check_batch_completion()

        return review

    def decline(self, reason: str = "", user=None):
        """
        Decline this assignment with an optional reason.
        """
        if self.status != AssignmentItemStatuses.PENDING:
            raise ValidationError(_("Can only decline pending assignments."))

        self.status = AssignmentItemStatuses.DECLINED
        self.responded_at = timezone.now()
        self.decline_reason = reason
        self.save(update_fields=["status", "responded_at", "decline_reason"])

        # Update batch status if all items are handled
        self._check_batch_completion()

        # Trigger auto-reassignment if configured
        self._handle_auto_reassignment()

    def _check_batch_completion(self):
        """Check if all items in the batch have been handled."""
        pending_count = self.batch.items.filter(
            status=AssignmentItemStatuses.PENDING
        ).count()
        if pending_count == 0:
            self.batch.status = AssignmentBatchStatuses.RESPONDED
            self.batch.responded_at = timezone.now()
            self.batch.save(update_fields=["status", "responded_at"])

    def _handle_auto_reassignment(self):
        """Handle automatic reassignment if configured."""
        try:
            config = self.batch.call.assignment_configuration
            if not config.auto_reassign_on_decline:
                return

            if self.reassign_count >= config.max_auto_reassign_attempts:
                logger.info(
                    f"Max reassignment attempts reached for proposal {self.proposal.uuid}"
                )
                return

            # TODO: Implement auto-reassignment logic
            # 1. Find next best reviewer from pool (by affinity, no COI)
            # 2. Create new AssignmentItem in a new or existing batch
            # 3. Send notification

        except CallAssignmentConfiguration.DoesNotExist:
            pass  # No auto-reassignment configured
