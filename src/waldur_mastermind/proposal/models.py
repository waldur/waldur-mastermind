import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Literal, cast

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker
from rest_framework.exceptions import ValidationError

import waldur_core.media.mixins
from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.configuration import config as constance_config
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core.enums import ReviewStates
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.permissions.enums import (
    SYSTEM_ROLE_SCOPES,
    TYPE_MAP,
    PermissionEnum,
    RoleEnum,
)
from waldur_core.permissions.mixins import PermissionMixin
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.common.serializers import (
    NON_INPUT_FIELD_TYPES,
    get_hidden_options,
)
from waldur_mastermind.marketplace import billing_mode
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.managers import (
    get_connected_offerings_by_permission,
)
from waldur_mastermind.marketplace.models import (
    SafeAttributesMixin,
    UserAttributeConfigBase,
)
from waldur_mastermind.proposal import enums
from waldur_mastermind.proposal.enums import (
    PROPOSAL_CONFIGURABLE_FIELDS,
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    AssignmentSources,
    BudgetEnforcement,
    CallStates,
    ChangeRequestParts,
    COIDetectionJobStates,
    COIDetectionJobTypes,
    COIDetectionMethods,
    COISeverityLevels,
    COIStatuses,
    COITypes,
    EvaluationStart,
    ExpertiseProficiencyLevels,
    FinancialInterestAmountRanges,
    FinancialInterestEntityTypes,
    FinancialInterestRelationshipTypes,
    MatchingAffinityMethods,
    MatchingAlgorithms,
    OrderAuthors,
    ProposalDisclosureLevels,
    ProposalFieldStates,
    ProposalStates,
    PublicationVenueTypes,
    RequestedOfferingStates,
    ResourceTemplateSelectionModes,
    ResultsPublication,
    ReviewerAffiliationTypes,
    ReviewerBidChoices,
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
    RoundLifecycleStates,
    RoundStatuses,
    SuggestionSourceTypes,
    UndecidedAtRoundCompletion,
)

from . import managers, proposal_roles, reviewer_standing
from . import status_labels as step_status_labels

logger = logging.getLogger(__name__)

# The column is unbounded text; this is the limit the applicant form shows and
# a ModelSerializer over Proposal inherits (DRF maps TextField.max_length), so
# create refuses a longer one. ProposalUpdateProjectDetailsSerializer declares
# its own field instead, to keep a value stored over the limit before it
# existed editable while unchanged, and ProposalAdmin drops it from its form
# for the same reason. Django itself does not validate it on a TextField, so
# ORM writes and clean_fields() stay free. Mirrored in homeport's
# PROPOSAL_FIELD_MAX_LENGTH.
PROJECT_SUMMARY_LENGTH = 1000

MISSING_RESOURCE_TEMPLATE_MESSAGE = _(
    "This call only accepts its resource templates. Replace the requests for "
    "these offerings with a template: %(offerings)s."
)
MISSING_SINGLE_TEMPLATE_MESSAGE = _(
    "This call needs one of its resource templates. Choose one before submitting."
)
TEMPLATE_OFFERING_NOT_ACCEPTED_MESSAGE = _(
    "The provider no longer accepts these offerings for this call: "
    "%(offerings)s. Choose a different template."
)
SEVERAL_TEMPLATES_MESSAGE = _(
    "This call allows one resource template per proposal. Keep one of them."
)
OUTDATED_TEMPLATE_REQUEST_MESSAGE = _(
    "These requests no longer match their resource template: %(templates)s. "
    "Choose the template again."
)
TEMPLATE_VALUES_LOCKED_MESSAGE = _(
    "A request made from a resource template takes its amounts and attributes "
    "from the template, so they cannot be set or changed. Choose a different "
    "template instead."
)
DUPLICATE_RESOURCE_TEMPLATE_MESSAGE = _(
    "A resource template is requested more than once on this proposal. Keep "
    "one request per template."
)
MISSING_PROPOSAL_MANAGER_MESSAGE = _(
    "A proposal needs at least one proposal manager to be submitted. Grant the "
    "proposal manager role to one of the team members."
)


def unanswered_compliance_message(count):
    return _(
        "Answer the required questions of the compliance checklist: "
        "%(count)s unanswered."
    ) % {"count": count}


ROUND_STEPS_NEED_CUTOFF_MESSAGE = (
    "The call has steps run once per round, which need the round's cut-off: "
    "evaluation starts at the cut-off."
)

CALL_NOT_ACTIVE_MESSAGE = _(
    "Call is not active, so the proposal can no longer be submitted."
)

EVALUATION_START_LOCKED_MESSAGE = _(
    "Cannot change when evaluation starts while the call has proposals that "
    "are submitted or in review."
)
PUBLISH_RESULTS_LOCKED_MESSAGE = _(
    "Cannot change when results are published while the call holds decisions "
    "that have not been published yet."
)
# Where a message shows a round's cut-off and the round has none. Plain text:
# mail contexts are serialised.
NO_CUTOFF_TEXT = "no cut-off (open until closed)"

# The most contributions a step may ask for or take, and the most holders an
# evaluator role may ask for or take on one proposal.
MAX_STEP_PEOPLE = 100


def people_bound_errors(**counts):
    """``{field: message}`` for each count above ``MAX_STEP_PEOPLE``."""
    return {
        name: f"Ensure this value is less than or equal to {MAX_STEP_PEOPLE}."
        for name, value in counts.items()
        if value is not None and value > MAX_STEP_PEOPLE
    }


OPEN_ENDED_ROUND_EVALUATION_MESSAGE = _(
    "A round without a cut-off needs evaluation to start on submission: "
    "proposals waiting for the cut-off would wait forever."
)
OPEN_ENDED_ROUND_PUBLICATION_MESSAGE = _(
    "A round without a cut-off needs results to be published immediately: "
    "decisions held for the round would never be published."
)
OPEN_ENDED_ROUND_ALLOCATION_MESSAGE = _(
    "A round without a cut-off cannot allocate on a fixed date: one date "
    "cannot fit proposals decided at any time."
)


DURATION_POLICIES_EXCLUSIVE_MESSAGE = _(
    "A call either fixes the duration of every project or lets the applicant "
    "choose it from a list, not both."
)


def duration_options_message(allowed):
    return _("Choose one of the durations the call offers: %(options)s months.") % {
        "options": ", ".join(str(months) for months in allowed)
    }


def normalize_applicant_cc_roles(value) -> list[str]:
    """The proposal roles a call copies on its applicant mail, by name:
    known proposal roles only, each once, in the order given."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError("A list of proposal role names.")
    names = list(dict.fromkeys(v.strip() for v in value if v.strip()))
    # The system roles of a proposal, created on first use, and any other
    # role defined on the proposal scope.
    known = {
        str(name)
        for name, scope in SYSTEM_ROLE_SCOPES.items()
        if scope == TYPE_MAP["proposal"]
    } | set(
        Role.objects.filter(
            name__in=names,
            content_type__app_label="proposal",
            content_type__model="proposal",
        ).values_list("name", flat=True)
    )
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValidationError(f"Not a proposal role: {', '.join(unknown)}.")
    return names


def normalize_allowed_duration_months(value) -> list[int]:
    """The call's list of durations an applicant may choose from, in months.

    Distinct whole numbers of at least one month, in ascending order; an
    empty list offers no choice. Raises ``ValidationError`` on anything else.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValidationError(_("A list of whole numbers of months is expected."))
    for months in value:
        if isinstance(months, bool) or not isinstance(months, int) or months < 1:
            raise ValidationError(
                _("Each duration is a whole number of months, at least 1.")
            )
        if months > 1200:
            raise ValidationError(_("A duration may be at most 1200 months."))
    return sorted(set(value))


RESOURCE_TEMPLATE_SELECTION_LOCKED_MESSAGE = _(
    "Cannot allow only one resource template while draft proposals hold "
    "several resource requests. Ask their applicants to keep one, then try "
    "again."
)


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
    max_active_proposals_per_applicant = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text=_(
            "How many proposals an applicant may have under evaluation or "
            "running as a project, across all of this organization's calls, "
            "before they may submit another. Empty sets no limit."
        ),
    )

    class Permissions:
        customer_path = "customer"

    class Meta:
        verbose_name = _("Call managing organization")

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
    waldur_core.media.mixins.ImageModelMixin,
    PermissionMixin,
):
    """Main entity representing calls for proposals with states (draft, active, archived). Contains configuration for reviewer visibility, review settings, and fixed duration parameters."""

    class Meta:
        ordering = ["-created", "id"]

    class States(CallStates):
        pass

    class OrderAuthor(OrderAuthors):
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
    allowed_duration_months = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Durations, in months, the applicant chooses the project's length "
            "from. Empty when the applicant is offered no choice. Exclusive "
            "with fixed_duration_in_days."
        ),
    )

    applicant_cc_roles = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Names of the proposal roles (PROPOSAL.MANAGER for the principal "
            "investigator, PROPOSAL.ADMIN, ...) whose holders are copied (Cc) "
            "on every mail to the applicant about the proposal. Empty copies "
            "nobody."
        ),
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
    project_name_template = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=enums.PROJECT_NAME_TEMPLATE_HELP,
    )

    panel_chair = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Panel member who chairs this call's review panel. Must hold the "
            "panel member role on the call; cleared automatically when that "
            "role is revoked. Addressable by notification rules as panel_chair."
        ),
    )

    panel_vice_chair = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Panel member who deputises for the panel chair. The same rules "
            "as the chair: must hold the panel member role on the call, is "
            "cleared when that role is revoked, and completes, contributes to "
            "and is notified for the steps of the panel chair."
        ),
    )

    order_author = models.CharField(
        max_length=20,
        choices=OrderAuthor.CHOICES,
        default=OrderAuthor.APPLICANT,
        help_text=(
            "Whose name the orders placed when this call grants resources "
            "carry. That person is who a helpdesk ticket is raised for and "
            "who Waldur's order mail is addressed to; reading the ticket in "
            "Waldur also needs a role on the project. The call review still "
            "authorizes the spend, and the orders are still carried out with "
            "system authority."
        ),
    )
    order_author_user = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "The person orders are attributed to when the author is set to a "
            "named contact. Useful for routing a whole call to a shared "
            "mailbox. Must hold a role on this call or on the organization "
            "managing it."
        ),
    )

    evaluation_start = models.CharField(
        max_length=20,
        choices=EvaluationStart.CHOICES,
        default=EvaluationStart.ON_SUBMISSION,
        help_text=(
            "When a submitted proposal's evaluation starts: at once on "
            "submission, or for every proposal of a round together at the "
            "round's cut-off. Cannot be changed while proposals are submitted "
            "or in review."
        ),
    )

    coi_declaration_text = models.TextField(
        blank=True,
        help_text=(
            "The conflict-of-interest declaration evaluators confirm, for every "
            "step asking them to that has no text of its own. Empty for the "
            "platform's standard declaration."
        ),
    )

    carry_over_drafts = models.BooleanField(
        default=True,
        help_text=(
            "Whether a draft still open when its round's cut-off passes moves "
            "on to the call's next round instead of being canceled. Without a "
            "later round the draft is canceled either way."
        ),
    )

    publish_results = models.CharField(
        max_length=20,
        choices=ResultsPublication.CHOICES,
        default=ResultsPublication.IMMEDIATELY,
        help_text=(
            "When applicants learn the allocation decision: as soon as it is "
            "made, or for every proposal of a round together when a call "
            "manager publishes the round's results. Cannot be changed while "
            "decisions are held."
        ),
    )

    undecided_at_round_completion = models.CharField(
        max_length=20,
        choices=UndecidedAtRoundCompletion.CHOICES,
        default=UndecidedAtRoundCompletion.REFUSE,
        help_text=(
            "What completing a round does with its proposals that still have "
            "no decision: refuse to complete the round, or reject them. A "
            "round may override it."
        ),
    )

    adoption_required = models.BooleanField(
        default=False,
        help_text=(
            "Whether a round's results must be adopted (by a board, say) "
            "before they are published. The adoption is then recorded while "
            "the round is deciding, and publication waits for it."
        ),
    )
    target_decision_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Days from a round's cut-off to its decision the workflow is "
            "planned for, when the round has no allocation date. The workflow "
            "check compares the steps' durations with it."
        ),
    )
    template = models.ForeignKey(
        "CallTemplate",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="calls",
        help_text="The call template the call was created from.",
    )
    template_version = models.PositiveIntegerField(
        null=True,
        blank=True,
        editable=False,
        help_text="Version of the template the call was created from.",
    )

    budget_enforcement = models.CharField(
        max_length=10,
        choices=BudgetEnforcement.CHOICES,
        default=BudgetEnforcement.SOFT,
        help_text=(
            "How the allocation decision is held to the call's resource "
            "budgets. soft: approving an award past what remains needs an "
            "explicit override with a reason. hard: such an award is refused, "
            "and only staff may override it with a reason. Changing it "
            "affects only later decisions."
        ),
    )
    resource_template_selection_mode = models.CharField(
        max_length=20,
        choices=ResourceTemplateSelectionModes.CHOICES,
        default=ResourceTemplateSelectionModes.MULTIPLE,
        help_text=(
            "Whether a proposal may request several of the call's resource "
            "templates or at most one of them. Cannot be set to single while "
            "a draft proposal holds several resource requests."
        ),
    )

    coi_configuration: "CallCOIConfiguration"
    round_set: models.Manager["Round"]

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

    def has_proposals(self):
        return Proposal.objects.filter(round__call=self).exists()

    def has_held_decisions(self):
        return Proposal.objects.filter(round__call=self, decision_held=True).exists()

    def has_proposals_under_evaluation(self):
        return Proposal.objects.filter(
            round__call=self,
            state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW],
        ).exists()

    def has_open_ended_round(self) -> bool:
        """Whether a round of the call has no cut-off (open until closed)."""
        return self.round_set.filter(cutoff_time__isnull=True).exists()

    def open_ended_round_conflict(
        self, evaluation_start=None, publish_results=None, allocation_time=None
    ):
        """Why the call cannot run a round without a cut-off, or None.

        Such a round never reaches a cut-off by itself, so nothing may wait
        for one: evaluation starts on submission, decisions are published as
        they are made, and allocation is not dated to the round. Arguments
        override the call's current settings.
        """
        if (evaluation_start or self.evaluation_start) != EvaluationStart.ON_SUBMISSION:
            return OPEN_ENDED_ROUND_EVALUATION_MESSAGE
        if (publish_results or self.publish_results) != ResultsPublication.IMMEDIATELY:
            return OPEN_ENDED_ROUND_PUBLICATION_MESSAGE
        if allocation_time is None and self.pk:
            step = CallWorkflowStep.allocation_decision_for(self)
            allocation_time = step.allocation_time if step else None
        if allocation_time == enums.AllocationTimes.FIXED_DATE:
            return OPEN_ENDED_ROUND_ALLOCATION_MESSAGE
        return None

    def allows_one_request_only(self):
        # A call with templates takes nothing else, so every request on it
        # stands for a template, even one whose template was since deleted.
        return (
            self.resource_template_selection_mode
            == ResourceTemplateSelectionModes.SINGLE
            and self.resource_templates.exists()
        )

    def has_drafts_with_several_requests(self):
        return (
            Proposal.objects.filter(round__call=self, state=ProposalStates.DRAFT)
            .annotate(request_count=models.Count("requestedresource"))
            .filter(request_count__gt=1)
            .exists()
        )

    def check_can_allow_one_request_only(self):
        """Refuse single selection while a draft holds several requests.

        The draft would break the rule the setting promises, and nothing would
        tell its applicant; the call manager sorts it out first.
        """
        if self.has_drafts_with_several_requests():
            raise ValidationError(
                {
                    "resource_template_selection_mode": RESOURCE_TEMPLATE_SELECTION_LOCKED_MESSAGE
                }
            )

    def clean(self):
        """Validate rules that span several fields or depend on related records."""
        try:
            self.allowed_duration_months = normalize_allowed_duration_months(
                self.allowed_duration_months
            )
        except ValidationError as e:
            raise ValidationError({"allowed_duration_months": e.detail})
        if self.allowed_duration_months and self.fixed_duration_in_days:
            raise ValidationError(
                {"allowed_duration_months": DURATION_POLICIES_EXCLUSIVE_MESSAGE}
            )
        try:
            self.applicant_cc_roles = normalize_applicant_cc_roles(
                self.applicant_cc_roles
            )
        except ValidationError as e:
            raise ValidationError({"applicant_cc_roles": e.detail})
        for field, label in (
            ("panel_chair", "Panel chair"),
            ("panel_vice_chair", "Panel vice-chair"),
        ):
            user_id = getattr(self, f"{field}_id")
            if (
                user_id
                and not get_users(self, RoleEnum.CALL_PANEL_MEMBER)
                .filter(pk=user_id)
                .exists()
            ):
                raise ValidationError(
                    {field: f"{label} must hold the panel member role on this call."}
                )
        if self.panel_vice_chair_id and self.panel_vice_chair_id == self.panel_chair_id:
            raise ValidationError(
                {
                    "panel_vice_chair": "The vice-chair cannot be the panel chair as well."
                }
            )
        if (
            self.pk
            and self.tracker.has_changed("evaluation_start")
            and self.has_proposals_under_evaluation()
        ):
            raise ValidationError({"evaluation_start": EVALUATION_START_LOCKED_MESSAGE})
        if (
            self.pk
            and self.tracker.has_changed("publish_results")
            and self.has_held_decisions()
        ):
            raise ValidationError({"publish_results": PUBLISH_RESULTS_LOCKED_MESSAGE})
        if (
            self.pk
            and self.tracker.has_changed("evaluation_start")
            and self.evaluation_start != EvaluationStart.AT_CUTOFF
            and self.workflow_steps.filter(round_scoped=True).exists()
        ):
            raise ValidationError({"evaluation_start": ROUND_STEPS_NEED_CUTOFF_MESSAGE})
        for field in ("evaluation_start", "publish_results"):
            if (
                self.pk
                and self.tracker.has_changed(field)
                and self.has_open_ended_round()
                and (conflict := self.open_ended_round_conflict())
            ):
                raise ValidationError({field: conflict})

        if (
            self.pk
            and self.tracker.has_changed("resource_template_selection_mode")
            and self.resource_template_selection_mode
            == ResourceTemplateSelectionModes.SINGLE
        ):
            self.check_can_allow_one_request_only()
        # The tracker covers every field, so it knows a foreign key by its attname.
        if (
            self.pk
            and self.tracker.has_changed("compliance_checklist_id")
            and self.has_proposals()
        ):
            raise ValidationError(
                "Cannot change compliance checklist when proposals exist"
            )
        # An empty template and no template both mean the default one.
        if (
            self.pk
            and (self.tracker.previous("proposal_slug_template") or None)
            != (self.proposal_slug_template or None)
            and self.has_proposals()
        ):
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


class CallProposalFieldConfig(TimeStampedModel, core_models.UuidMixin):
    """Which Project details fields this call asks for, and which it insists on.

    One row per call, seeded at call creation from the Constance defaults (see
    ``handlers.seed_proposal_field_config``) rather than resolved lazily. A call
    that resolved its defaults at read time would tighten retroactively the day
    an operator raised the installation default, which the locking rule below
    exists to prevent.

    ``name`` is deliberately absent: it names the proposal and goes into the
    awarded project's name (see ``allocation_names``, which
    applies the call's ``project_name_template``). The length of the award is
    not a form field either: it is derived at allocation from the requested
    resources' ``attributes.prepaid_duration_months``, else from
    ``call.fixed_duration_in_days`` (see ``utils.project_end_date``).
    """

    FIELD_PREFIX = "field_"

    call = models.OneToOneField(
        Call,
        on_delete=models.CASCADE,
        related_name="proposal_field_config",
    )

    field_project_summary = models.CharField(
        max_length=10,
        choices=ProposalFieldStates.CHOICES,
        default=ProposalFieldStates.REQUIRED,
    )
    field_description = models.CharField(
        max_length=10,
        choices=ProposalFieldStates.CHOICES,
        default=ProposalFieldStates.OPTIONAL,
    )
    field_science_sub_domain = models.CharField(
        max_length=10,
        choices=ProposalFieldStates.CHOICES,
        default=ProposalFieldStates.OPTIONAL,
    )
    field_supporting_documentation = models.CharField(
        max_length=10,
        choices=ProposalFieldStates.CHOICES,
        default=ProposalFieldStates.OPTIONAL,
    )

    def __str__(self):
        return f"Proposal field config for {self.call}"

    @classmethod
    def field_names(cls) -> list[str]:
        return list(PROPOSAL_CONFIGURABLE_FIELDS)

    @classmethod
    def column_for(cls, field_name: str) -> str:
        return f"{cls.FIELD_PREFIX}{field_name}"

    @classmethod
    def default_states(cls) -> dict[str, str]:
        """Installation defaults, as a field -> state map.

        Read from Constance so an operator can set the house style once, and
        applied only when a call is created. Anything neither required nor
        hidden is optional.
        """
        required = set(constance_config.DEFAULT_PROPOSAL_REQUIRED_FIELDS or [])
        hidden = set(constance_config.DEFAULT_PROPOSAL_HIDDEN_FIELDS or [])
        states = {}
        for field_name in cls.field_names():
            if field_name in hidden:
                states[field_name] = ProposalFieldStates.HIDDEN
            elif field_name in required:
                states[field_name] = ProposalFieldStates.REQUIRED
            else:
                states[field_name] = ProposalFieldStates.OPTIONAL
        return states

    def get_states(self) -> dict[str, str]:
        return {
            field_name: getattr(self, self.column_for(field_name))
            for field_name in self.field_names()
        }

    @classmethod
    def get_states_for_call(cls, call) -> dict[str, str]:
        """States for a call, falling back to the model defaults.

        Calls created before this model existed have no row; they keep the
        behaviour the form had before it was configurable.
        """
        config = getattr(call, "proposal_field_config", None)
        if config is not None:
            return config.get_states()
        return {
            field_name: cls._meta.get_field(cls.column_for(field_name)).default
            for field_name in cls.field_names()
        }


class CallProposalRole(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """An evaluator role a call gives on each of its proposals.

    For example a lead and a second rapporteur. The call manager defines the
    vocabulary; holders are assigned per proposal (``ProposalRoleAssignment``),
    in an assignment step or by a call manager. This is not the applicant
    team: proposal-scoped ``UserRole`` grants are, and holders of these roles
    never join it.
    """

    class Permissions:
        customer_path = "call__manager__customer"

    call = models.ForeignKey(
        "Call",
        on_delete=models.CASCADE,
        related_name="proposal_roles",
    )
    key = models.SlugField(
        max_length=64,
        help_text="Identifier of the role within its call.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    eligible_from = models.CharField(
        max_length=32,
        choices=enums.ProposalRoleEligibility.CHOICES,
        default=enums.ProposalRoleEligibility.CALL_PANEL_MEMBER,
        help_text="Who may hold the role.",
    )
    min_holders = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text=(
            "How many holders an assignment step for this role needs before "
            "it can complete."
        ),
    )
    max_holders = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="How many people may hold the role on one proposal. Empty: no limit.",
    )
    allows_lead = models.BooleanField(
        default=False,
        help_text="Whether one holder is marked as the lead.",
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Evaluator role")
        verbose_name_plural = _("Evaluator roles")
        constraints = [
            models.UniqueConstraint(
                fields=["call", "key"], name="proposal_unique_call_proposal_role_key"
            ),
        ]

    def __str__(self):
        return f"{self.call.name} — {self.name}"

    def steps_beyond_holders(self, max_holders):
        """The call's steps that need more contributions from this role's
        holders than ``max_holders`` of them can give."""
        if max_holders is None:
            return []
        return [
            step
            for step in self.call.workflow_steps.filter(
                min_contributions__isnull=False,
                contributor_source__startswith=(
                    f"{enums.ContributorSources.PROPOSAL_ROLE}:"
                ),
            )
            if (role := proposal_roles.resolve_role(self.call, step.contributor_source))
            is not None
            and role.pk == self.pk
            and step.min_contributions
            > step.contribution_capacity(self, max_holders=max_holders)
        ]

    def lead_only_steps(self):
        """The call's steps only this role's lead contributes to."""
        return [
            step
            for step in self.call.workflow_steps.filter(contributors_lead_only=True)
            if (role := proposal_roles.resolve_role(self.call, step.contributor_source))
            is not None
            and role.pk == self.pk
        ]

    @classmethod
    def unique_key_for(cls, call_id, name, exclude_pk=None):
        """A slug of ``name`` not yet used by another role of the call."""
        base = slugify(name)[:56] or "role"
        taken = set(
            cls.objects.filter(call_id=call_id, key__startswith=base)
            .exclude(pk=exclude_pk)
            .values_list("key", flat=True)
        )
        key, suffix = base, 2
        while key in taken:
            key = f"{base}-{suffix}"
            suffix += 1
        return key

    def save(self, *args, **kwargs):
        if not self.key and self.call_id:
            self.key = self.unique_key_for(self.call_id, self.name, exclude_pk=self.pk)
        super().save(*args, **kwargs)

    def clean(self):
        errors = people_bound_errors(
            min_holders=self.min_holders, max_holders=self.max_holders
        )
        if errors:
            raise DjangoValidationError(errors)
        if (
            self.max_holders is not None
            and self.min_holders is not None
            and self.min_holders > self.max_holders
        ):
            raise DjangoValidationError(
                {"min_holders": "Cannot be more than the maximum number of holders."}
            )


class CallNotificationOverride(TimeStampedModel, core_models.UuidMixin):
    """A call's own wording of one mail it sends: subject, text and HTML
    bodies replacing the portal's template for that mail on this call.

    ``mail_type`` names the notification (``workflow_step_event``,
    ``proposal_state_changed``...) and ``variant`` the audience it is worded
    for where one notification reaches several (the applicant, the call
    team); see ``notification_overrides.MAIL_TYPES``. The bodies are
    rendered in a sandbox with the same context the recipient's mail has.
    """

    class Permissions:
        customer_path = "call__manager__customer"

    call = models.ForeignKey(
        "Call",
        on_delete=models.CASCADE,
        related_name="notification_overrides",
    )
    mail_type = models.CharField(max_length=64)
    variant = models.CharField(max_length=32, blank=True, default="")
    subject = models.TextField()
    body_text = models.TextField()
    body_html = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["mail_type", "variant", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["call", "mail_type", "variant"],
                name="proposal_unique_call_notification_override",
            ),
        ]

    def __str__(self):
        return f"{self.call.name} — {self.mail_type}:{self.variant}"


class CallPanel(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """A panel of a call, such as a Domain Panel covering some research domains.

    Proposals are routed to a panel by their science sub-domain when their
    evaluation starts (``panels.route``); evaluator roles eligible from
    ``panel:chair`` or ``panel:member`` are then held by the chair or the
    members of the proposal's panel. Membership grants nothing by itself:
    every member must hold the panel member role on the call, and one who
    loses it is taken off the call's panels.
    """

    class Permissions:
        customer_path = "call__manager__customer"

    call = models.ForeignKey(
        "Call",
        on_delete=models.CASCADE,
        related_name="panels",
    )
    key = models.SlugField(
        max_length=64,
        help_text="Identifier of the panel within its call.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(
        default=0, help_text="Position of the panel in the call's list."
    )
    chair = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The chair of the panel, one of its members.",
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="+",
        help_text="Panel members of the call who sit on this panel.",
    )
    science_domains = models.ManyToManyField(
        "structure.ScienceDomain",
        blank=True,
        related_name="+",
        help_text="Proposals of these domains are routed to the panel.",
    )
    science_sub_domains = models.ManyToManyField(
        "structure.ScienceSubDomain",
        blank=True,
        related_name="+",
        help_text=(
            "Proposals of these sub-domains are routed to the panel, whichever "
            "panel their domain maps to."
        ),
    )

    proposals: models.Manager["Proposal"]

    class Meta:
        ordering = ["order", "id"]
        verbose_name = _("Call panel")
        verbose_name_plural = _("Call panels")
        constraints = [
            models.UniqueConstraint(
                fields=["call", "key"], name="proposal_unique_call_panel_key"
            ),
        ]

    def __str__(self):
        return f"{self.call.name} — {self.name}"

    @classmethod
    def unique_key_for(cls, call_id, name, exclude_pk=None):
        """A slug of ``name`` not yet used by another panel of the call."""
        base = slugify(name)[:56] or "panel"
        taken = set(
            cls.objects.filter(call_id=call_id, key__startswith=base)
            .exclude(pk=exclude_pk)
            .values_list("key", flat=True)
        )
        key, suffix = base, 2
        while key in taken:
            key = f"{base}-{suffix}"
            suffix += 1
        return key

    def save(self, *args, **kwargs):
        if not self.key and self.call_id:
            self.key = self.unique_key_for(self.call_id, self.name, exclude_pk=self.pk)
        super().save(*args, **kwargs)


class CallWorkflowStep(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """A step of a call's evaluation workflow.

    The call manager composes the workflow from ordered, named steps, each of
    one code-defined kind (``enums.STEP_KINDS``); a kind may appear several
    times. Proposals walk the enabled steps by ``order``. Authorisation for
    acting on a step is resolved through the parent Call's role assignments —
    see ``permissions.user_can_act_on_step``.
    """

    class Permissions:
        customer_path = "call__manager__customer"

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name="workflow_steps",
    )
    key = models.SlugField(
        max_length=64,
        help_text=(
            "Identifier of the step within its call. Steps the call was seeded "
            "with keep the keys of the former step catalog."
        ),
    )
    kind = models.CharField(
        max_length=32,
        choices=enums.StepKinds.CHOICES,
        help_text="Behavior of the step.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(
        help_text="Position of the step in the call's workflow.",
    )
    outcome_set = models.CharField(
        max_length=32,
        choices=enums.OutcomeSets.CHOICES,
        help_text="Outcomes the step can be completed with.",
    )
    on_negative = models.CharField(
        max_length=16,
        choices=enums.OnNegativeOutcomes.CHOICES,
        default=enums.OnNegativeOutcomes.REJECT,
        help_text="What a negative outcome does to the proposal.",
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text="Whether this step is enabled. Disabled steps are skipped.",
    )
    duration_in_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration in days. Used to calculate deadlines.",
    )

    # Evaluation form (reuses Checklist system)
    # Protected: a step without its form loses what its contributors filled
    # in. Detach the form from the step first.
    checklist = models.ForeignKey(
        "checklist.Checklist",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="Evaluation form for this step.",
    )
    checklist_required = models.BooleanField(
        default=True,
        help_text=(
            "When the step has a checklist, block completion until its required "
            "questions are answered. Set False to make the checklist advisory."
        ),
    )

    # Review behavior
    blind_review = models.BooleanField(
        default=False,
        help_text="Evaluators cannot see each other's assessments.",
    )
    requires_coi_confirmation = models.BooleanField(
        default=False,
        help_text="Evaluator must confirm absence of conflict of interest.",
    )
    coi_declaration_text = models.TextField(
        blank=True,
        help_text=(
            "The conflict-of-interest declaration this step's evaluators "
            "confirm; empty for the call's, or the platform's standard one."
        ),
    )
    min_contributions = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Minimum contributions submitted before the step can complete. On a "
            "step without contributors, the minimum of the proposal's submitted "
            "reviews."
        ),
    )
    max_contributions = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Most contributions the step accepts; no limit when empty.",
    )
    contributor_source = models.CharField(
        max_length=64,
        default=enums.ContributorSources.NONE,
        help_text=(
            "Who contributes to the step, each with a contribution of their "
            "own: none, requested_offering_team, assigned_reviewers, "
            "proposal_role:<id> or call_role:<role>."
        ),
    )
    per_requested_offering = models.BooleanField(
        default=False,
        help_text=(
            "One contribution per offering the proposal requests, which any "
            "member of that offering's team may fill."
        ),
    )
    contributors_lead_only = models.BooleanField(
        default=False,
        help_text=(
            "Only the lead of the evaluator role the contributor source names "
            "contributes: one slot, the lead's, which moves when the lead "
            "changes. The step waits while the role has no lead."
        ),
    )
    delegable_to = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "Who a contribution slot of the step can be handed to by its "
            "holder or the call's managers: empty for nobody, offering_team "
            "for anyone with a role on the slot's own offering (per-offering "
            "steps), or proposal_role:<id> for the holders of an evaluator role "
            "on the proposal (per-person steps). The person it is handed to "
            "accepts or declines it."
        ),
    )
    min_score_threshold = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Minimum average score required before this step can complete "
            "with any outcome but declined (a completion gate; it does not "
            "auto-reject lower scores). A step with contributors scores its "
            "own submitted reviews. A step without contributors that reads "
            "input steps takes the score of the latest completed input step "
            "that has one: the average of its reviews, else of the rating "
            "answers in its checklists. Otherwise all the proposal's submitted "
            "reviews count."
        ),
    )

    # Visibility
    applicant_visible = models.BooleanField(
        default=False,
        help_text="Whether the applicant can see step details (not just status).",
    )

    # Responsibility and transition behavior
    completer = models.CharField(
        max_length=32,
        choices=enums.ResponsibleRoles.CHOICES,
        null=True,
        blank=True,
        help_text="Role expected to complete this step.",
    )
    completer_proposal_role = models.ForeignKey(
        CallProposalRole,
        # A role a step relies on cannot go; deleting the call takes both.
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="completed_steps",
        help_text=(
            "The evaluator role whose holders complete this step, when the "
            "completer is proposal_role."
        ),
    )
    assigns_roles = models.ManyToManyField(
        CallProposalRole,
        blank=True,
        related_name="assigning_steps",
        help_text=(
            "Assignment step: the evaluator roles whose holders the completer "
            "picks in this step, such as a lead and a second rapporteur."
        ),
    )
    input_steps = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="output_steps",
        help_text=(
            "Earlier steps of the call whose outputs -- contributions, reviews, "
            "form answers and outcome -- this step's contributors and completer "
            "read. On an applicant step, the applicant reads their public parts."
        ),
    )
    release_to_applicant = models.BooleanField(
        default=False,
        help_text=(
            "Assessment step: once completed, its report can be released to "
            "the applicant, such as a consolidated report or final feedback."
        ),
    )
    collects_reviewer_suggestions = models.BooleanField(
        default=False,
        help_text=(
            "Assessment step of evaluator-role holders: each holder may "
            "suggest reviewers for the proposal while the step is active. "
            "The call's managers turn suggestions into pool invitations."
        ),
    )
    allows_conditional_acceptance = models.BooleanField(
        default=True,
        help_text=(
            "Per-offering assessment: a contribution may accept a requested "
            "offering under conditions. Off, it accepts or rejects it."
        ),
    )
    allows_change_requests = models.BooleanField(
        default=False,
        help_text=(
            "Whoever acts on the step may send the proposal back to the "
            "applicant to change the parts they unlock, as often as needed. "
            "The proposal stays in review on the step meanwhile. Not for the "
            "allocation decision nor an applicant step."
        ),
    )
    transition_mode = models.CharField(
        max_length=32,
        choices=enums.TransitionModes.CHOICES,
        default=enums.TransitionModes.AUTOMATIC_ON_COMPLETION,
        help_text=(
            "How this step advances once a human completes it. 'Automatic' "
            "advances to the next step immediately; 'Manual' waits for a "
            "separate advance action. Neither auto-decides from review scores."
        ),
    )
    run_condition = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "When the step runs: empty to run on every proposal, "
            '{"step": <key of an earlier step>, "outcomes": [...]} to run only '
            "when that step ended with one of the outcomes, or one of its "
            "contributions did, or "
            '{"step": <key of an earlier step>, "score_spread_above": <points>} '
            "to run only when the scores its contributions gave the proposal "
            "lie more than that many points apart. Otherwise the proposal "
            "skips it."
        ),
    )
    parallel_with_previous = models.BooleanField(
        default=False,
        help_text=(
            "Start together with the step before it, as one stage: every step "
            "of a stage starts at once, and the next stage starts when all "
            "of them have ended."
        ),
    )
    round_scoped = models.BooleanField(
        default=False,
        help_text=(
            "Run the step once per round rather than per proposal, such as a "
            "panel meeting after the cut-off: the call's managers open it for "
            "the round's proposals together, its contributors write one round "
            "report each covering their proposals, and it is completed for "
            "the round, never on one proposal."
        ),
    )
    phase = models.CharField(
        max_length=16,
        choices=enums.WorkflowPhases.CHOICES,
        default=enums.WorkflowPhases.EVALUATION,
        help_text=(
            "When the step runs: in the evaluation, from submission to the "
            "award; in an extension, which the applicant of an accepted "
            "proposal asks for as often as needed; or in the closing, the "
            "final report at the end of the allocation. A phase's steps run "
            "in their order, each extension and closing in a run of its own."
        ),
    )
    project_end_offset_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "First step of an extension or closing phase: days before the "
            "project's end date. An extension can be asked for until then; "
            "the closing opens then (0: on the end date)."
        ),
    )
    status_labels = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "What each audience's dashboard calls the step, as entered: "
            '{"applicant": {...}, "evaluators": {...}, "call_team": {...}, '
            '"offering_team": {...}}, each with optional '
            "'active', 'completed' and 'changes_requested' texts and "
            "'outcomes' ({<outcome>: text}). Left out, the computed label "
            "stays; the call team's and the offering teams' fall back to the "
            "evaluators'. The applicant's apply only while the step is "
            "visible to the applicant. Other labels may show counts of the "
            "step's work: {invited}, {accepted}, {declined}, {submitted}, "
            "{expected}, {reports_submitted}, {reports_expected}."
        ),
    )

    # Allocation decision
    is_allocation_decision = models.BooleanField(
        default=False,
        help_text=(
            "The decision step that settles the award. A call is activated "
            "with exactly one enabled."
        ),
    )
    include_award_response = models.BooleanField(
        default=False,
        help_text=(
            "Allocation decision: require applicant award response after decision."
        ),
    )
    decides_automatically = models.BooleanField(
        default=False,
        help_text=(
            "Allocation decision: approve on reaching this step, as the "
            "system, with the award as requested; fall back to the completer "
            "when the approval is refused."
        ),
    )
    automatic_decision_guard = models.CharField(
        max_length=20,
        choices=enums.AutomaticDecisionGuards.CHOICES,
        default=enums.AutomaticDecisionGuards.EARLIER_STEP,
        help_text=(
            "Automatic decision: what stops an unsuitable proposal -- an "
            "enabled earlier step that can reject it (earlier_step), or a "
            "person at the provider acting on every order the award places, "
            "on every accepted offering of the call (provider_fulfilment)."
        ),
    )
    allocation_time = models.CharField(
        max_length=15,
        choices=enums.AllocationTimes.CHOICES,
        default=enums.AllocationTimes.ON_DECISION,
        help_text=(
            "Allocation decision: when a granted proposal takes effect — "
            "immediately (on_decision) or on the round's allocation date "
            "(fixed_date)."
        ),
    )
    display_order = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Deprecated: steps are ordered by order.",
    )

    # The largest position a call manager can give a step.
    MAX_ORDER = 32767

    class Meta:
        ordering = ["order", "id"]
        verbose_name = _("Workflow step")
        verbose_name_plural = _("Workflow steps")
        constraints = [
            models.UniqueConstraint(
                fields=["call", "key"], name="proposal_unique_call_step_key"
            ),
            models.UniqueConstraint(
                fields=["call", "order"], name="proposal_unique_call_step_order"
            ),
        ]

    def __str__(self):
        return f"{self.call.name} — {self.name or self.key}"

    @property
    def responsible_role(self):
        """Deprecated name of ``completer``, kept for one release."""
        return self.completer

    @responsible_role.setter
    def responsible_role(self, value):
        self.completer = value

    @property
    def min_reviewers(self):
        """Deprecated name of ``min_contributions``, kept for one release."""
        return self.min_contributions

    @min_reviewers.setter
    def min_reviewers(self, value):
        self.min_contributions = value

    @property
    def kind_definition(self):
        return enums.STEP_KINDS_MAP.get(self.kind)

    @property
    def contributor_source_kind(self):
        """The contributor source without its parameter."""
        try:
            return enums.parse_contributor_source(self.contributor_source)[0]
        except ValueError:
            return enums.ContributorSources.NONE

    @property
    def collects_contributions(self):
        """Whether the step's work is counted in contributions of named people."""
        return self.contributor_source_kind != enums.ContributorSources.NONE

    @property
    def continues_on_negative(self):
        return self.on_negative == enums.OnNegativeOutcomes.CONTINUE

    @property
    def derives_outcome(self):
        """Whether the step outcome follows from its contributions' outcomes."""
        return self.outcome_set in enums.OutcomeSets.DERIVED

    @property
    def contribution_outcomes(self):
        """Outcomes a contribution to the step may conclude with."""
        return enums.ContributionOutcomes.for_step(
            self.outcome_set, self.allows_conditional_acceptance
        )

    @property
    def allowed_outcomes(self):
        return enums.OutcomeSets.OUTCOMES.get(self.outcome_set, ())

    @property
    def cancels_on_negative(self):
        return self.on_negative == enums.OnNegativeOutcomes.CANCEL

    @property
    def can_end_negatively(self):
        """Whether completing the step can end the proposal's workflow: an
        assessment or decision with a negative outcome that does not let the
        proposal go on."""
        return (
            self.kind in (enums.StepKinds.ASSESSMENT, enums.StepKinds.DECISION)
            and not self.continues_on_negative
            and any(
                outcome in enums.WorkflowStepOutcomes.NEGATIVE_OUTCOMES
                for outcome in self.allowed_outcomes
            )
        )

    @property
    def relies_on_provider_fulfilment(self):
        """Whether the automatic decision is guarded by the provider acting
        on every order, rather than by an earlier step."""
        return (
            self.decides_automatically
            and self.automatic_decision_guard
            == enums.AutomaticDecisionGuards.PROVIDER_FULFILMENT
        )

    @classmethod
    def unguarded_automatic_decision(cls, call_id):
        """The call's enabled automatic allocation decision guarded by an
        earlier step when no enabled step before it can reject a proposal,
        else None: every submission would award itself. One relying on the
        provider fulfilment is checked on the call's offerings
        (``utils.provider_fulfilment_refusal``)."""
        steps = list(
            cls.objects.filter(call_id=call_id, is_enabled=True).order_by("order", "id")
        )
        for step in steps:
            if step.is_allocation_decision and step.decides_automatically:
                return None if step.relies_on_provider_fulfilment else step
            if step.can_end_negatively:
                return None
        return None

    def apply_kind_defaults(self):
        """Fill in what the kind decides when the caller left it out.

        Done here rather than in ``__init__`` so instances hydrated from the
        database are never mutated.
        """
        kind = self.kind_definition
        if kind is None:
            return
        if not self.completer:
            self.completer = kind.default_completer
        if not self.outcome_set:
            self.outcome_set = kind.default_outcome_set
        if not self.name:
            self.name = kind.name
        if not self.key and self.call_id:
            self.key = self.unique_key_for(self.call_id, self.name, exclude_pk=self.pk)
        if self.order is None and self.call_id:
            self.order = self.next_order(self.call_id)

    @classmethod
    def unique_key_for(cls, call_id, name, exclude_pk=None):
        """A slug of ``name`` not yet used by another step of the call."""
        base = slugify(name)[:56] or "step"
        taken = set(
            cls.objects.filter(call_id=call_id, key__startswith=base)
            .exclude(pk=exclude_pk)
            .values_list("key", flat=True)
        )
        key, suffix = base, 2
        while key in taken:
            key = f"{base}-{suffix}"
            suffix += 1
        return key

    @classmethod
    def next_order(cls, call_id):
        """The order after the call's last step.

        Should that pass ``MAX_ORDER``, the call's steps are renumbered from 0
        first: proposals keep the order they were submitted with, so only the
        configuration moves.
        """
        orders = list(
            cls.objects.filter(call_id=call_id)
            .order_by("order", "id")
            .values_list("pk", "order")
        )
        if not orders:
            return 0
        if orders[-1][1] >= cls.MAX_ORDER:
            for index, (pk, _) in enumerate(orders):
                cls.objects.filter(pk=pk).update(order=index)
            return len(orders)
        return orders[-1][1] + 1

    @classmethod
    def default_order_for(cls, call_id, key):
        """Where a new step goes when nobody says, freeing the place it takes.

        A default step goes back to where it stands among the call's other
        default steps, so a step that was deleted and is added again keeps its
        place; any other step is appended. The award response goes right after
        the allocation decision, ahead of anything that follows the award.
        """
        if key == enums.AWARD_RESPONSE_KEY:
            # The decision that runs: a disabled one before it would put the
            # response ahead of the award it answers.
            decision = (
                cls.objects.filter(call_id=call_id, is_allocation_decision=True)
                .order_by("-is_enabled", "order")
                .first()
            )
            if decision is not None:
                cls.make_room(call_id, decision.order + 1)
                return decision.order + 1
        keys = [step.key for step in enums.DEFAULT_CALL_STEPS]
        if key in keys:
            successor = (
                cls.objects.filter(call_id=call_id, key__in=keys[keys.index(key) + 1 :])
                .order_by("order")
                .first()
            )
            if successor is not None:
                cls.make_room(call_id, successor.order)
                return successor.order
        return cls.next_order(call_id)

    @classmethod
    def create_default(cls, call, key, **values):
        """Create the call's step from the default step of ``key``.

        ``values`` override the default's fields. The step takes its default
        place unless ``values`` give an order.
        """
        default = enums.DEFAULT_CALL_STEPS_MAP[key]
        fields = {
            "kind": default.kind,
            "name": default.name,
            "description": default.description,
            "outcome_set": default.outcome_set,
            "completer": default.completer,
            "on_negative": default.on_negative,
            "is_allocation_decision": default.is_allocation_decision,
            "contributor_source": default.contributor_source,
        }
        fields.update(values)
        if fields.get("order") is None:
            fields["order"] = cls.default_order_for(call.pk, key)
        else:
            cls.make_room(call.pk, fields["order"])
        return cls.objects.create(call=call, key=key, **fields)

    @classmethod
    def make_room(cls, call_id, order, exclude_pk=None):
        """Free ``order`` in the call by moving it and every later step down one.

        ``exclude_pk`` is a step about to take ``order`` itself: it is parked
        past the end first, out of the way of the steps moving down. Rows are
        moved last first, so ``unique(call, order)`` holds after every single
        update.
        """
        steps = cls.objects.filter(call_id=call_id).exclude(pk=exclude_pk)
        if not steps.filter(order=order).exists():
            return
        if exclude_pk is not None:
            last = steps.order_by("-order").values_list("order", flat=True).first()
            cls.objects.filter(pk=exclude_pk).update(order=last + 2)
        for pk, current in (
            steps.filter(order__gte=order).order_by("-order").values_list("pk", "order")
        ):
            cls.objects.filter(pk=pk).update(order=current + 1)

    @classmethod
    def award_response_precedes_decision(cls, call_id, changed=None):
        """Whether the call's enabled award response would come before its
        enabled allocation decision.

        ``changed`` describes one step as it is about to be saved (``pk``,
        ``key``, ``order``, ``is_enabled``, ``is_allocation_decision``), so a
        change is checked before it lands. A step taking an order already
        held moves there and pushes the steps from it down one, as
        ``make_room`` does.
        """
        rows = {
            step["pk"]: dict(step)
            for step in cls.objects.filter(call_id=call_id).values(
                "pk", "key", "order", "is_enabled", "is_allocation_decision"
            )
        }
        if changed is not None:
            changed = dict(changed)
            current = rows.pop(changed.get("pk"), None)
            order = changed.get("order")
            if order is None:
                order = (
                    current["order"]
                    if current
                    else max((r["order"] for r in rows.values()), default=-1) + 1
                )
            elif current is None or order != current["order"]:
                for row in rows.values():
                    if row["order"] >= order:
                        row["order"] += 1
            changed["order"] = order
            rows[changed.get("pk")] = changed
        enabled = [row for row in rows.values() if row["is_enabled"]]
        decisions = [row["order"] for row in enabled if row["is_allocation_decision"]]
        responses = [
            row["order"] for row in enabled if row["key"] == enums.AWARD_RESPONSE_KEY
        ]
        return bool(decisions and responses) and min(responses) < min(decisions)

    @classmethod
    def allocation_decision_for(cls, call):
        """The call's enabled allocation decision step, if any."""
        return cls.objects.filter(
            call=call, is_allocation_decision=True, is_enabled=True
        ).first()

    @classmethod
    def review_step_for(cls, call):
        """The call's assessment that collects peer reviews.

        Its ``min_contributions`` is the call's "enough reviewers" threshold.
        The first enabled step whose contributors are the assigned reviewers,
        else the first one at all; failing both, the first step with the
        reviewed outcome set.
        """
        steps = cls.objects.filter(call=call, kind=enums.StepKinds.ASSESSMENT)
        return (
            steps.filter(contributor_source=enums.ContributorSources.ASSIGNED_REVIEWERS)
            .order_by("-is_enabled", "order", "id")
            .first()
            or steps.filter(outcome_set=enums.OutcomeSets.REVIEWED)
            .order_by("-is_enabled", "order", "id")
            .first()
        )

    def save(self, *args, **kwargs):
        self.apply_kind_defaults()
        super().save(*args, **kwargs)
        if self.key == enums.AWARD_RESPONSE_KEY or self.is_allocation_decision:
            self.sync_award_response_flag(self.call_id)

    def delete(self, *args, **kwargs):
        call_id, key = self.call_id, self.key
        result = super().delete(*args, **kwargs)
        if key == enums.AWARD_RESPONSE_KEY:
            self.sync_award_response_flag(call_id)
        return result

    @classmethod
    def sync_award_response_flag(cls, call_id):
        """Keep the allocation decision's ``include_award_response`` saying
        whether the call has an enabled award response step.

        The flag is how the award response used to be added and removed; the
        step is now added, disabled and deleted on its own, and the flag would
        otherwise keep saying there is one after it is gone -- and put it back
        on the next write that carries the flag as it reads it.
        """
        included = cls.objects.filter(
            call_id=call_id, key=enums.AWARD_RESPONSE_KEY, is_enabled=True
        ).exists()
        cls.objects.filter(call_id=call_id, is_allocation_decision=True).exclude(
            include_award_response=included
        ).update(include_award_response=included)

    @property
    def responds_to_inputs(self) -> bool:
        """Whether the step asks the applicant to respond to earlier steps'
        outputs, such as a response to reviews: an applicant step whose
        outcome is a submission and that reads earlier steps."""
        return (
            self.kind == enums.StepKinds.APPLICANT_ACTION
            and self.outcome_set == enums.OutcomeSets.SUBMITTED
            and self.input_steps.exists()
        )

    @classmethod
    def undecided_response_steps(cls, call_id, changed=None, removed_pk=None):
        """Names of the call's response steps no decision follows.

        A response step lets the workflow go on whether or not the applicant
        answers, so something must decide after it: an enabled decision step
        later in the call. ``changed`` is a step about to be saved, as a
        mapping of ``pk`` (None for a new one), ``name``, ``order`` (None to
        append), ``is_enabled``, ``kind``, ``outcome_set`` and ``has_inputs``;
        ``removed_pk`` a step about to be deleted.
        """
        rows = {
            step.pk: {
                "pk": step.pk,
                "name": step.name,
                "order": step.order,
                "is_enabled": step.is_enabled,
                "kind": step.kind,
                "outcome_set": step.outcome_set,
                "has_inputs": step.has_inputs,
                "conditional": bool(step.run_condition),
            }
            for step in cls.objects.filter(call_id=call_id).annotate(
                has_inputs=models.Exists(
                    cls.input_steps.through.objects.filter(
                        from_callworkflowstep_id=models.OuterRef("pk")
                    )
                )
            )
        }
        rows.pop(removed_pk, None)
        if changed is not None:
            changed = dict(changed)
            current = rows.pop(changed["pk"], None)
            order = changed.get("order")
            if order is None:
                order = (
                    current["order"]
                    if current
                    else max((r["order"] for r in rows.values()), default=-1) + 1
                )
            elif current is None or order != current["order"]:
                # A step taking a place moves the step there, and the ones
                # after it, down one.
                for row in rows.values():
                    if row["order"] >= order:
                        row["order"] += 1
            changed["order"] = order
            rows[changed["pk"]] = changed
        enabled = [row for row in rows.values() if row["is_enabled"]]
        # A decision that runs only on some proposals decides nothing for
        # the others.
        decisions = [
            row["order"]
            for row in enabled
            if row["kind"] == enums.StepKinds.DECISION
            and not row.get("conditional", False)
        ]
        return sorted(
            row["name"]
            for row in enabled
            if row["kind"] == enums.StepKinds.APPLICANT_ACTION
            and row["outcome_set"] == enums.OutcomeSets.SUBMITTED
            and row["has_inputs"]
            and not any(order > row["order"] for order in decisions)
        )

    @classmethod
    def check_responses_decided(cls, call_id, changed=None, removed_pk=None):
        undecided = cls.undecided_response_steps(call_id, changed, removed_pk)
        if undecided:
            raise DjangoValidationError(
                {
                    "input_steps": (
                        f"No decision step follows the response step "
                        f"'{undecided[0]}': whether the applicant answers or "
                        "not, the workflow goes on to a decision."
                    )
                }
            )

    @classmethod
    def roles_assigned_too_late(cls, call_id, removed_pk=None):
        """(step, role, assignment) for each enabled step whose evaluator role
        -- its contributors' or its completer's -- an assignment step of the
        call assigns only after it: the proposal would reach the step before
        anyone could hold the role. A role no assignment step assigns is left
        to the call managers, who assign it on each proposal."""
        steps = list(
            cls.objects.filter(call_id=call_id, is_enabled=True)
            .exclude(pk=removed_pk)
            .select_related("call", "completer_proposal_role")
            .prefetch_related("assigns_roles")
            .order_by("order", "id")
        )
        first = {}
        for step in steps:
            if step.kind != enums.StepKinds.ASSIGNMENT:
                continue
            for role in step.assigns_roles.all():
                first.setdefault(role.pk, (role, step))
        late = []
        for step in steps:
            if step.kind == enums.StepKinds.ASSIGNMENT:
                continue
            roles = []
            if step.contributor_source_kind == enums.ContributorSources.PROPOSAL_ROLE:
                roles.append(
                    proposal_roles.resolve_role(step.call, step.contributor_source)
                )
            if step.completer == enums.ResponsibleRoles.PROPOSAL_ROLE:
                roles.append(step.completer_proposal_role)
            for role in {r.pk: r for r in roles if r is not None}.values():
                if role.pk in first and first[role.pk][1].order > step.order:
                    late.append((step, role, first[role.pk][1]))
        return late

    @classmethod
    def check_roles_assigned_first(cls, call_id, removed_pk=None):
        late = cls.roles_assigned_too_late(call_id, removed_pk)
        if late:
            step, role, assignment = late[0]
            raise DjangoValidationError(
                {
                    "order": (
                        f"Step '{step.name}' is the work of the evaluator role "
                        f"'{role.name}', which step '{assignment.name}' assigns "
                        "only after it: move the assignment before it, or take "
                        f"the role out of '{assignment.name}' to have the call "
                        "managers assign it on each proposal."
                    )
                }
            )

    @staticmethod
    def stages(steps):
        """Map each of ``steps`` -- one call's, in ``(order, id)`` order -- to
        its stage: the order of the first step of its run of steps flagged
        ``parallel_with_previous``. A first step's flag starts nothing to run
        beside, so it opens a stage of its own, as does a step of another
        phase than the one before it: phases never run together."""
        stages, current, phase = {}, None, None
        for step in steps:
            if (
                current is None
                or not step.parallel_with_previous
                or step.phase != phase
            ):
                current = step.order
            stages[step.pk] = current
            phase = step.phase
        return stages

    @classmethod
    def check_stages_and_conditions(cls, call_id, removed_pk=None):
        """Refuse a workflow whose stages or run conditions cannot be walked.

        Checked on the call's steps as saved -- the caller's transaction rolls
        a refused change back -- or, with ``removed_pk``, as they would stand
        without that step. A stage of several enabled steps holds neither the
        allocation decision, which the award waits on alone, nor an applicant
        step, as the applicant does one thing at a time; and none of its
        steps reads another of it, whose work is not done yet. A run
        condition names a step of an earlier stage and outcomes that step or
        its contributions can end with; the allocation decision runs on every
        proposal.
        """
        # A decision that runs only on some proposals does not decide after a
        # response step for the others.
        cls.check_responses_decided(call_id, removed_pk=removed_pk)
        steps = list(
            cls.objects.filter(call_id=call_id)
            .exclude(pk=removed_pk)
            .prefetch_related("input_steps")
            .order_by("order", "id")
        )
        stages = cls.stages(steps)
        members = {}
        for step in steps:
            if step.is_enabled:
                members.setdefault(stages[step.pk], []).append(step)
        for stage_steps in members.values():
            if len(stage_steps) < 2:
                continue
            for step in stage_steps:
                if step.is_allocation_decision:
                    raise DjangoValidationError(
                        {
                            "parallel_with_previous": (
                                f"The allocation decision '{step.name}' runs "
                                "alone, not alongside another step."
                            )
                        }
                    )
                if step.kind == enums.StepKinds.APPLICANT_ACTION:
                    raise DjangoValidationError(
                        {
                            "parallel_with_previous": (
                                f"The applicant step '{step.name}' runs alone, "
                                "not alongside another step."
                            )
                        }
                    )
                for input_step in step.input_steps.all():
                    if input_step.pk != step.pk and stages.get(
                        input_step.pk
                    ) == stages.get(step.pk):
                        raise DjangoValidationError(
                            {
                                "parallel_with_previous": (
                                    f"Step '{step.name}' reads step "
                                    f"'{input_step.name}', which runs alongside "
                                    "it; a step reads only earlier stages."
                                )
                            }
                        )
        by_key = {step.key: step for step in steps}
        for step in steps:
            condition = step.run_condition or {}
            if not condition:
                continue
            if step.is_allocation_decision:
                raise DjangoValidationError(
                    {
                        "run_condition": (
                            "The allocation decision runs on every proposal; "
                            "it cannot have a run condition."
                        )
                    }
                )
            source = by_key.get(condition.get("step"))
            if source is None:
                raise DjangoValidationError(
                    {
                        "run_condition": (
                            f"Step '{step.name}' runs on the outcome of step "
                            f"'{condition.get('step')}', which the call does "
                            "not have."
                        )
                    }
                )
            if stages[source.pk] >= stages[step.pk]:
                raise DjangoValidationError(
                    {
                        "run_condition": (
                            f"Step '{step.name}' runs on the outcome of step "
                            f"'{source.name}', which does not end before it."
                        )
                    }
                )
            if enums.SCORE_SPREAD_ABOVE in condition:
                if not source.collects_contributions:
                    raise DjangoValidationError(
                        {
                            "run_condition": (
                                f"Step '{step.name}' runs on how far apart the "
                                f"scores of step '{source.name}' lie, which "
                                "has no contributors to score the proposal."
                            )
                        }
                    )
                continue
            known = source.condition_outcomes
            unknown = [o for o in condition.get("outcomes") or [] if o not in known]
            if unknown:
                raise DjangoValidationError(
                    {
                        "run_condition": (
                            f"Step '{source.name}' cannot end with "
                            f"{', '.join(unknown)}; use one of "
                            f"{', '.join(known)}."
                        )
                    }
                )

    @property
    def condition_outcomes(self):
        """Outcomes a later step's run condition may name on this step: its
        own, its contributions', and its lapse."""
        outcomes = [*self.allowed_outcomes, *self.contribution_outcomes]
        outcomes.append(enums.WorkflowStepOutcomes.EXPIRED)
        return list(dict.fromkeys(outcomes))

    @staticmethod
    def check_reader_order(step_pk, order):
        """Refuse moving a step to ``order`` after a step reading it; one
        standing at ``order`` moves down, out of its way."""
        if step_pk is None or order is None:
            return
        reader = (
            CallWorkflowStep.objects.filter(input_steps__pk=step_pk, order__lt=order)
            .exclude(pk=step_pk)
            .order_by("order")
            .first()
        )
        if reader is not None:
            raise DjangoValidationError(
                {
                    "order": (
                        f"Step '{reader.name}' reads this step's outputs, so "
                        "this step must stay before it."
                    )
                }
            )

    @staticmethod
    def check_input_steps(step_pk, call_id, order, inputs, phase=None):
        """Refuse input steps a step cannot read: another call's, itself, or
        one that does not come before it; and, by ``phase``, a step of an
        extension or closing reads the evaluation or its own phase, while
        the evaluation reads only itself.

        A many-to-many relation is set after the row is saved, so ``clean()``
        cannot see it: whoever sets it checks here first. ``order`` is the
        step's own position; None for a step appended at the end.
        """
        phase = phase or enums.WorkflowPhases.EVALUATION
        for step in inputs or []:
            if step.phase not in (enums.WorkflowPhases.EVALUATION, phase):
                raise DjangoValidationError(
                    {
                        "input_steps": (
                            f"Step '{step.name}' runs in the {step.phase} phase; "
                            "a step reads the evaluation and its own phase only."
                        )
                    }
                )
            if call_id and step.call_id != call_id:
                raise DjangoValidationError(
                    {"input_steps": "An input step belongs to another call."}
                )
            if step_pk is not None and step.pk == step_pk:
                raise DjangoValidationError(
                    {"input_steps": "A step cannot read its own outputs."}
                )
            if order is not None and step.order >= order:
                raise DjangoValidationError(
                    {
                        "input_steps": (
                            f"Step '{step.name}' does not come before this "
                            "step; only earlier steps can be read."
                        )
                    }
                )

    @staticmethod
    def check_assigned_roles(kind_id, call_id, roles):
        """Refuse evaluator roles a step of ``kind_id`` in ``call_id`` cannot
        assign: any, unless it is an assignment step; another call's always.

        A many-to-many relation is set after the row is saved, so ``clean()``
        cannot see it: whoever sets it checks here first.
        """
        roles = list(roles or [])
        if roles and kind_id != enums.StepKinds.ASSIGNMENT:
            raise DjangoValidationError(
                {"assigns_roles": "Only an assignment step assigns evaluator roles."}
            )
        if any(call_id and role.call_id != call_id for role in roles):
            raise DjangoValidationError(
                {"assigns_roles": "An evaluator role belongs to another call."}
            )

    def clean(self):
        """Validate the step against its kind and the call's other steps."""
        kind = self.kind_definition
        if kind is None:
            return
        if self.outcome_set and self.outcome_set not in kind.outcome_sets:
            raise DjangoValidationError(
                {
                    "outcome_set": (
                        f"A {kind.name.lower()} step cannot use the "
                        f"'{self.outcome_set}' outcome set. Allowed: "
                        f"{', '.join(kind.outcome_sets)}."
                    )
                }
            )
        if (
            self.completer_proposal_role is not None
            and self.call_id
            and self.completer_proposal_role.call_id != self.call_id
        ):
            raise DjangoValidationError(
                {
                    "completer_proposal_role": (
                        "The evaluator role belongs to another call."
                    )
                }
            )
        if (self.completer == enums.ResponsibleRoles.PROPOSAL_ROLE) != bool(
            self.completer_proposal_role_id
        ):
            raise DjangoValidationError(
                {
                    "completer_proposal_role": (
                        "Name the evaluator role exactly when the step is "
                        "completed by the holders of one."
                    )
                }
            )
        if (
            self.completer == enums.ResponsibleRoles.APPLICANT
            and kind.id != enums.StepKinds.APPLICANT_ACTION
        ):
            # The applicant would assess, assign or decide on their own
            # proposal: its eligibility, its reviewers, its award.
            raise DjangoValidationError(
                {
                    "completer": (
                        "The applicant completes only an applicant action; a "
                        f"{kind.name.lower()} step is the call team's."
                    )
                }
            )
        if self.is_allocation_decision and kind.id != enums.StepKinds.DECISION:
            raise DjangoValidationError(
                {"is_allocation_decision": "Only a decision step settles the award."}
            )
        if self.decides_automatically and not self.is_allocation_decision:
            raise DjangoValidationError(
                {
                    "decides_automatically": (
                        "Only the allocation decision decides automatically."
                    )
                }
            )
        if (
            self.automatic_decision_guard != enums.AutomaticDecisionGuards.EARLIER_STEP
            and not self.decides_automatically
        ):
            raise DjangoValidationError(
                {
                    "automatic_decision_guard": (
                        "Only an automatic decision relies on what stops an "
                        "unsuitable proposal."
                    )
                }
            )
        if (
            self.decides_automatically
            and self.transition_mode == enums.TransitionModes.MANUAL
        ):
            raise DjangoValidationError(
                {
                    "transition_mode": (
                        "An automatic decision moves on by itself; it cannot "
                        "wait for a manual advance."
                    )
                }
            )
        if self.release_to_applicant and kind.id != enums.StepKinds.ASSESSMENT:
            raise DjangoValidationError(
                {
                    "release_to_applicant": (
                        "Only an assessment step's report is released to the applicant."
                    )
                }
            )
        if self.allows_change_requests and (
            self.is_allocation_decision or kind.id == enums.StepKinds.APPLICANT_ACTION
        ):
            # The award changes through the decision itself, and a send-back
            # there would tell the applicant a decision is pending; an
            # applicant step is the applicant's already.
            raise DjangoValidationError(
                {
                    "allows_change_requests": (
                        "Changes are requested of the applicant on an "
                        "evaluation step, not on the allocation decision or "
                        "an applicant step."
                    )
                }
            )
        self._clean_contributions(kind)
        self._clean_run_condition()
        self._clean_round_scoped(kind)
        self._clean_phase()
        self.status_labels = step_status_labels.clean(
            self.status_labels, self.outcome_set
        )
        if self.is_allocation_decision and not self.is_enabled:
            raise DjangoValidationError(
                f"Step '{self.name}' is the allocation decision and cannot be disabled."
            )
        if (
            self.is_allocation_decision
            and self.is_enabled
            and self.call_id
            and CallWorkflowStep.objects.filter(
                call_id=self.call_id, is_allocation_decision=True, is_enabled=True
            )
            .exclude(pk=self.pk)
            .exists()
        ):
            raise DjangoValidationError(
                {
                    "is_allocation_decision": (
                        "The call already has an allocation decision step."
                    )
                }
            )

    def round_scoped_refusal(self, kind=None, evaluation_start=None):
        """Why the step cannot run once per round, or None.

        A round step is an assessment written as round reports by the holders
        of a role, or a decision the call's managers take for the round. It
        is never the allocation decision, which settles each award, nor work
        counted per contribution or per offering. Its batch needs the
        round's cut-off, so the call evaluates at the cut-off.
        """
        kind = kind or self.kind_definition
        if kind is None or kind.id not in (
            enums.StepKinds.ASSESSMENT,
            enums.StepKinds.DECISION,
        ):
            return "Only an assessment or a decision step runs once per round."
        if self.is_post_award:
            return (
                "A step after the award runs on one accepted proposal, never "
                "once per round."
            )
        if self.is_allocation_decision:
            return (
                "The allocation decision settles each proposal's award; it "
                "cannot run once per round."
            )
        if self.per_requested_offering:
            return (
                "A step assessed per requested offering runs on each "
                "proposal, not once per round."
            )
        if self.min_contributions is not None or self.max_contributions is not None:
            return (
                "A round step is done when its round reports are submitted: "
                "leave the minimum and maximum of contributions empty."
            )
        if self.min_score_threshold is not None:
            return "A round step has no score threshold."
        if self.delegable_to:
            return "A round report is written by its author; it is not handed on."
        if self.collects_reviewer_suggestions or self.allows_change_requests:
            return (
                "A round step neither collects reviewer suggestions nor sends "
                "proposals back to their applicant."
            )
        source = self.contributor_source_kind
        if source not in (
            enums.ContributorSources.NONE,
            enums.ContributorSources.PROPOSAL_ROLE,
            enums.ContributorSources.CALL_ROLE,
        ):
            return (
                "A round step's reports are written by the holders of an "
                "evaluator role or of a call role, or by nobody: set the "
                "contributor source to none, proposal_role:<id> or "
                "call_role:<role>."
            )
        if evaluation_start is None and self.call_id:
            evaluation_start = self.call.evaluation_start
        if evaluation_start and evaluation_start != EvaluationStart.AT_CUTOFF:
            return (
                "A round step needs the round's cut-off: set the call to start "
                "evaluation at the cut-off."
            )
        return None

    def _clean_round_scoped(self, kind):
        if not self.round_scoped:
            return
        refusal = self.round_scoped_refusal(kind)
        if refusal:
            raise DjangoValidationError({"round_scoped": refusal})

    def _clean_phase(self):
        """A step of an extension or closing runs on an accepted proposal:
        it settles no award, cancels nothing and runs on every run of its
        phase. Changes it asks of the applicant are to the run's own parts
        (``change_requests.run_unlockable_parts``)."""
        if not self.is_post_award:
            if self.project_end_offset_days is not None:
                raise DjangoValidationError(
                    {
                        "project_end_offset_days": (
                            "Only a step of an extension or closing is timed "
                            "from the project's end date."
                        )
                    }
                )
            return
        refused = {
            "is_allocation_decision": self.is_allocation_decision,
            "include_award_response": self.include_award_response,
            "decides_automatically": self.decides_automatically,
            "run_condition": bool(self.run_condition),
        }
        for field, value in refused.items():
            if value:
                raise DjangoValidationError(
                    {
                        field: (
                            "A step of an extension or closing runs on an "
                            "accepted proposal; this belongs to the evaluation."
                        )
                    }
                )
        if self.cancels_on_negative:
            raise DjangoValidationError(
                {
                    "on_negative": (
                        "A negative outcome of an extension or closing step "
                        "closes its run, never the accepted proposal: choose "
                        "reject or continue."
                    )
                }
            )

    @property
    def is_post_award(self) -> bool:
        """Whether the step runs on an accepted proposal, in a run."""
        return self.phase in enums.WorkflowPhases.POST_AWARD

    @classmethod
    def phase_steps(cls, call_id, phase):
        """The call's enabled steps of ``phase``, in their order."""
        return cls.objects.filter(
            call_id=call_id, phase=phase, is_enabled=True
        ).order_by("order", "id")

    @classmethod
    def phase_structure_errors(cls, call_id):
        """What keeps the call's extension and closing phases from running.

        An extension, if it has enabled steps, opens with the applicant's
        request -- an applicant step with the submitted outcome -- and ends
        with a decision that approves or declines it. A closing opens with
        the applicant's final report, an applicant step with the submitted
        outcome, and ends with it or with a decision that approves it.
        Checked when the call is activated: a phase is built one step at a
        time.
        """
        errors = []
        for phase, label, needs_decision in (
            (enums.WorkflowPhases.EXTENSION, "extension", True),
            (enums.WorkflowPhases.CLOSING, "closing", False),
        ):
            steps = list(cls.phase_steps(call_id, phase))
            if not steps:
                continue
            first, last = steps[0], steps[-1]
            if not (
                first.kind == enums.StepKinds.APPLICANT_ACTION
                and first.outcome_set == enums.OutcomeSets.SUBMITTED
            ):
                errors.append(
                    f"The {label} opens with an applicant step whose outcome "
                    f"is a submission; '{first.name}' is not one."
                )
            if needs_decision or len(steps) > 1:
                if not (
                    last.kind == enums.StepKinds.DECISION
                    and last.outcome_set == enums.OutcomeSets.APPROVAL
                ):
                    errors.append(
                        f"The {label} ends with a decision that approves or "
                        f"declines it; '{last.name}' is not one."
                    )
            for step in steps[1:]:
                if step.kind == enums.StepKinds.APPLICANT_ACTION:
                    errors.append(
                        f"The {label} has one applicant step, its first; "
                        f"'{step.name}' is another."
                    )
        return errors

    def _clean_run_condition(self):
        """A run condition is empty, or names a step and either the outcomes
        of it the step runs on or how far apart its contributions' scores
        must lie, and nothing else."""
        condition = self.run_condition
        if condition in (None, {}):
            self.run_condition = {}
            return
        outcomes = condition.get("outcomes") if isinstance(condition, dict) else None
        spread = (
            condition.get(enums.SCORE_SPREAD_ABOVE)
            if isinstance(condition, dict)
            else None
        )
        if (
            isinstance(condition, dict)
            and set(condition) == {"step", enums.SCORE_SPREAD_ABOVE}
            and isinstance(condition["step"], str)
            and condition["step"]
        ):
            if (
                isinstance(spread, bool)
                or not isinstance(spread, int | float)
                or not 0 <= spread <= enums.MAX_SCORE_SPREAD
            ):
                raise DjangoValidationError(
                    {
                        "run_condition": (
                            "The score spread is a number of points from 0 to "
                            f"{enums.MAX_SCORE_SPREAD}."
                        )
                    }
                )
        elif (
            not isinstance(condition, dict)
            or set(condition) != {"step", "outcomes"}
            or not isinstance(condition["step"], str)
            or not condition["step"]
            or not isinstance(outcomes, list)
            or not outcomes
            or not all(isinstance(o, str) and o for o in outcomes)
        ):
            raise DjangoValidationError(
                {
                    "run_condition": (
                        'A run condition is {"step": <step key>, "outcomes": '
                        '[<outcome>, ...]}, {"step": <step key>, '
                        '"score_spread_above": <points>}, or empty to run on '
                        "every proposal."
                    )
                }
            )
        if condition["step"] == self.key:
            raise DjangoValidationError(
                {"run_condition": "A step cannot run on its own outcome."}
            )

    def _clean_lead_only(self, source):
        """Lead-only contributors need an evaluator role that has a lead."""
        if source != enums.ContributorSources.PROPOSAL_ROLE:
            raise DjangoValidationError(
                {
                    "contributors_lead_only": (
                        "Only the lead of an evaluator role can be the sole "
                        "contributor: set the contributor source to "
                        "proposal_role:<id>."
                    )
                }
            )
        if not self.call_id:
            return
        role = proposal_roles.resolve_role(self.call, self.contributor_source)
        if role is not None and not role.allows_lead:
            raise DjangoValidationError(
                {
                    "contributors_lead_only": (
                        f"The evaluator role '{role.name}' has no lead; only a "
                        "role whose holders include a lead can limit the "
                        "contributors to it."
                    )
                }
            )

    def contribution_capacity(self, role, max_holders=None):
        """How many contributions the holders of ``role`` can give the step:
        one from its lead on a lead-only step, else one per holder. None
        when the role takes any number of holders. ``max_holders`` stands
        for the role's own while it is being changed."""
        if self.contributors_lead_only:
            return 1
        return role.max_holders if max_holders is None else max_holders

    def _clean_contribution_capacity(self, source):
        """Refuse a minimum of contributions the step's evaluator role can
        never reach: the proposal would wait at the step for good."""
        if (
            source != enums.ContributorSources.PROPOSAL_ROLE
            or not self.min_contributions
            or not self.call_id
        ):
            return
        role = proposal_roles.resolve_role(self.call, self.contributor_source)
        if role is None:
            return
        capacity = self.contribution_capacity(role)
        if capacity is None or self.min_contributions <= capacity:
            return
        who = (
            f"only the lead of the evaluator role '{role.name}' contributes"
            if self.contributors_lead_only
            else f"the evaluator role '{role.name}' has at most {capacity} "
            "holder(s) on a proposal"
        )
        raise DjangoValidationError(
            {
                "min_contributions": (
                    f"The step needs {self.min_contributions} contributions, "
                    f"but {who}: lower the minimum, or let more holders "
                    "contribute."
                )
            }
        )

    def _clean_contributions(self, kind):
        """Validate who contributes to the step and how their work counts."""
        try:
            source, parameter = enums.parse_contributor_source(self.contributor_source)
        except ValueError as exc:
            raise DjangoValidationError({"contributor_source": str(exc)})
        if source == enums.ContributorSources.PROPOSAL_ROLE and not (
            enums.is_proposal_role_ref(parameter)
        ):
            raise DjangoValidationError(
                {
                    "contributor_source": (
                        "An evaluator role is named by its id or uuid: "
                        "proposal_role:<id or uuid>."
                    )
                }
            )
        if self.contributors_lead_only:
            self._clean_lead_only(source)
        self._clean_contribution_capacity(source)
        if (
            source != enums.ContributorSources.NONE
            and kind.id != enums.StepKinds.ASSESSMENT
        ):
            raise DjangoValidationError(
                {"contributor_source": "Only an assessment step has contributors."}
            )
        if self.per_requested_offering and (
            source != enums.ContributorSources.REQUESTED_OFFERING_TEAM
        ):
            raise DjangoValidationError(
                {
                    "per_requested_offering": (
                        "A contribution per requested offering is filled by the "
                        "offering's team: set the contributor source to "
                        "requested_offering_team."
                    )
                }
            )
        if self.derives_outcome and not self.per_requested_offering:
            raise DjangoValidationError(
                {
                    "outcome_set": (
                        "The per-offering outcome set needs a contribution per "
                        "requested offering."
                    )
                }
            )
        if (
            self.collects_reviewer_suggestions
            and source != enums.ContributorSources.PROPOSAL_ROLE
        ):
            raise DjangoValidationError(
                {
                    "collects_reviewer_suggestions": (
                        "Reviewers are suggested by the holders of an evaluator "
                        "role: set the contributor source to proposal_role:<id>."
                    )
                }
            )
        if (
            self.collects_reviewer_suggestions
            and self.completer != enums.ResponsibleRoles.PROPOSAL_ROLE
        ):
            # The completer reads every suggestion, with the suggested
            # people's e-mail addresses: only the holders of an evaluator
            # role, resolved per proposal, may be given that.
            raise DjangoValidationError(
                {
                    "completer": (
                        "A step collecting reviewer suggestions is completed by "
                        "the holders of an evaluator role: set the completer "
                        "to proposal_role."
                    )
                }
            )
        errors = people_bound_errors(
            min_contributions=self.min_contributions,
            max_contributions=self.max_contributions,
        )
        if errors:
            raise DjangoValidationError(errors)
        if kind.id in (
            enums.StepKinds.ASSIGNMENT,
            enums.StepKinds.APPLICANT_ACTION,
        ) and (self.min_contributions or self.max_contributions is not None):
            # Such a step would count the proposal's reviews, which are
            # written later, if ever: it could never complete.
            field = (
                "min_contributions" if self.min_contributions else "max_contributions"
            )
            raise DjangoValidationError(
                {
                    field: (
                        f"A {kind.name.lower()} step takes no contributions "
                        "or reviews: leave the minimum and maximum empty."
                    )
                }
            )
        if self.per_requested_offering and self.max_contributions is not None:
            # One slot per requested offering: a maximum below their number
            # refuses the last offerings' assessments, and leaves the step
            # waiting for them for good.
            raise DjangoValidationError(
                {
                    "max_contributions": (
                        "A step assessed per requested offering takes one "
                        "contribution from each of them: leave the maximum empty."
                    )
                }
            )
        if (
            self.min_contributions is not None
            and self.max_contributions is not None
            and self.min_contributions > self.max_contributions
        ):
            raise DjangoValidationError(
                {
                    "max_contributions": (
                        "The maximum cannot be below the minimum of contributions."
                    )
                }
            )
        if self.continues_on_negative and self.is_allocation_decision:
            raise DjangoValidationError(
                {
                    "on_negative": (
                        "A negative allocation decision ends the workflow; it "
                        "cannot continue to another step."
                    )
                }
            )
        self._clean_delegation(source)

    def _clean_delegation(self, source):
        """Validate who the step's slots can be handed to: the team of the
        slot's offering on a per-offering step, the holders of another
        evaluator role of the call on a step of per-person slots."""
        try:
            target, role_ref = enums.parse_delegation_target(self.delegable_to)
        except ValueError as exc:
            raise DjangoValidationError({"delegable_to": str(exc)})
        if target == enums.DelegationTargets.NONE:
            return
        if target == enums.DelegationTargets.OFFERING_TEAM:
            if not self.per_requested_offering:
                raise DjangoValidationError(
                    {
                        "delegable_to": (
                            "Only a step with a contribution per requested "
                            "offering hands its slots to the offering's team."
                        )
                    }
                )
            return
        if self.per_requested_offering or source not in (
            enums.ContributorSources.PROPOSAL_ROLE,
            enums.ContributorSources.CALL_ROLE,
        ):
            raise DjangoValidationError(
                {
                    "delegable_to": (
                        "Only a step whose slots belong to the holders of a "
                        "role hands them to the holders of an evaluator role."
                    )
                }
            )
        if not enums.is_proposal_role_ref(role_ref):
            raise DjangoValidationError(
                {
                    "delegable_to": (
                        "An evaluator role is named by its id or uuid: "
                        "proposal_role:<id or uuid>."
                    )
                }
            )
        if not self.call_id:
            return
        role = proposal_roles.resolve_role(self.call, self.delegable_to)
        if role is None:
            raise DjangoValidationError(
                {"delegable_to": "The evaluator role is not one of the call's."}
            )
        if source == enums.ContributorSources.PROPOSAL_ROLE:
            contributing = proposal_roles.resolve_role(
                self.call, self.contributor_source
            )
            if contributing is not None and contributing.pk == role.pk:
                raise DjangoValidationError(
                    {
                        "delegable_to": (
                            "The slots already belong to the holders of this "
                            "role; name another evaluator role."
                        )
                    }
                )


def is_internal_step(call_step):
    """Whether the applicant is kept out of the step's progress.

    Evaluation is internal: the applicant only ever hears about the
    allocation decision, about the steps that are theirs to complete, and
    about a decision that is a verdict on them in its own right -- one whose
    details the call shows them, or one closing a run they asked for after
    the award (an extension, the final report).
    """
    if call_step.is_allocation_decision:
        return False
    if call_step.completer == enums.ResponsibleRoles.APPLICANT:
        return False
    kind = call_step.kind_definition
    if kind and kind.applicant_facing:
        return False
    if call_step.kind == enums.StepKinds.DECISION and (
        call_step.applicant_visible
        or call_step.phase in enums.WorkflowPhases.POST_AWARD
    ):
        return False
    return True


def is_applicant_recipient(recipient, call_step):
    """True when ``recipient`` on ``call_step`` is the applicant side."""
    if recipient == enums.NotificationRuleRecipients.APPLICANT:
        return True
    return (
        recipient == enums.NotificationRuleRecipients.RESPONSIBLE_ROLE
        and call_step.completer == enums.ResponsibleRoles.APPLICANT
    )


class CallWorkflowStepNotificationRule(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Call-level rule: on a workflow event for a step, e-mail an audience.

    Rules are configuration owned by the call manager, seeded per call from
    the built-in defaults (see ``handlers.seed_notification_rules``) and
    resolved against a concrete proposal only when the event fires
    (``notification_rules.dispatch_step_event``). ``days_before`` is the lead
    time for ``deadline_approaching`` and is meaningless for the other
    triggers, which fire the moment the step changes status.
    """

    class Permissions:
        customer_path = "workflow_step__call__manager__customer"

    workflow_step = models.ForeignKey(
        CallWorkflowStep,
        on_delete=models.CASCADE,
        related_name="notification_rules",
    )
    trigger = models.CharField(
        max_length=32,
        choices=enums.NotificationRuleTriggers.CHOICES,
    )
    recipient = models.CharField(
        max_length=32,
        choices=enums.NotificationRuleRecipients.CHOICES,
    )
    days_before = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Only for deadline_approaching: how many days before the step's "
            "deadline the reminder is sent."
        ),
    )
    is_enabled = models.BooleanField(default=True)
    notified_proposal_roles = models.ManyToManyField(
        Role,
        blank=True,
        related_name="+",
        help_text=(
            "Only for an applicant audience: the proposal roles whose holders "
            "are notified. Empty notifies the proposal creator and every "
            "member of the proposal team."
        ),
    )
    proposal_role = models.ForeignKey(
        CallProposalRole,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="notification_rules",
        help_text=(
            "Only for the proposal_role and proposal_role_lead audiences: the "
            "evaluator role whose holders, or whose lead, are notified."
        ),
    )

    class Meta:
        constraints = [
            # One rule per audience, an evaluator role being an audience of
            # its own. Two partial constraints rather than one that treats
            # NULLs as equal, which older PostgreSQL cannot express.
            models.UniqueConstraint(
                fields=["workflow_step", "trigger", "recipient"],
                condition=Q(proposal_role__isnull=True),
                name="proposal_unique_step_notification_rule",
            ),
            models.UniqueConstraint(
                fields=["workflow_step", "trigger", "recipient", "proposal_role"],
                condition=Q(proposal_role__isnull=False),
                name="proposal_unique_step_role_notification_rule",
            ),
        ]
        ordering = ["created", "id"]
        verbose_name = _("Workflow step notification rule")
        verbose_name_plural = _("Workflow step notification rules")

    def __str__(self):
        return f"{self.workflow_step} — {self.trigger} → {self.recipient}"

    @property
    def call(self):
        return self.workflow_step.call

    def clean(self):
        if self.trigger == enums.NotificationRuleTriggers.DEADLINE_APPROACHING:
            if self.days_before is None:
                raise DjangoValidationError(
                    {"days_before": "Required for the deadline_approaching trigger."}
                )
        elif self.days_before is not None:
            raise DjangoValidationError(
                {"days_before": "Only applies to the deadline_approaching trigger."}
            )
        if (
            self.recipient in enums.NotificationRuleRecipients.NAMING_PROPOSAL_ROLE
        ) != bool(self.proposal_role_id):
            raise DjangoValidationError(
                {
                    "proposal_role": (
                        "Name the evaluator role exactly when the rule notifies "
                        "the holders of one, or its lead."
                    )
                }
            )
        if (
            self.recipient == enums.NotificationRuleRecipients.PROPOSAL_ROLE_LEAD
            and self.proposal_role_id
            and not self.proposal_role.allows_lead
        ):
            raise DjangoValidationError(
                {"proposal_role": "The evaluator role has no lead to notify."}
            )
        if (
            self.proposal_role_id
            and self.workflow_step_id
            and self.proposal_role.call_id != self.workflow_step.call_id
        ):
            raise DjangoValidationError(
                {"proposal_role": "The evaluator role belongs to another call."}
            )
        if (
            self.workflow_step_id
            and is_applicant_recipient(self.recipient, self.workflow_step)
            and is_internal_step(self.workflow_step)
        ):
            raise DjangoValidationError(
                {
                    "recipient": (
                        "The applicant cannot be notified about internal "
                        "evaluation steps."
                    )
                }
            )


class WorkflowCriterion(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Named evaluation criterion attached to a workflow step (e.g. expert review)."""

    workflow_step = models.ForeignKey(
        CallWorkflowStep,
        on_delete=models.CASCADE,
        related_name="criteria",
    )
    name = models.CharField(max_length=255)
    order = models.PositiveSmallIntegerField(default=0)
    weight = models.PositiveSmallIntegerField(
        default=1,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(enums.CRITERION_WEIGHT_MAX),
        ],
        help_text=(
            "The criterion's weight in a review's overall score, relative to "
            "the other criteria of the step."
        ),
    )

    class Meta:
        ordering = ["order", "id"]
        unique_together = [("workflow_step", "name")]
        verbose_name = _("Workflow criterion")
        verbose_name_plural = _("Workflow criteria")

    def __str__(self):
        return f"{self.workflow_step} — {self.name}"


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

        @staticmethod
        def build_query(user):
            # An offering manager answers the requests for their offering
            # without any role on the provider organization.
            return Q(
                offering__in=get_connected_offerings_by_permission(
                    user, PermissionEnum.ACCEPT_REQUESTED_OFFERING
                )
            )

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
    # Protected: deleting the plan would delete the call's offering with it,
    # and with that every request and award on it.
    plan = models.ForeignKey(
        on_delete=models.PROTECT, to=marketplace_models.Plan, null=True, blank=True
    )
    rejection_reason = models.TextField(
        blank=True,
        default="",
        help_text="Why the provider declined to take part in the call.",
    )
    default_delegate = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Who the provider hands its per-offering slots on to as they open, "
            "on steps whose slots may be handed within the offering's team. "
            "They accept or decline each, as for a slot handed on by hand."
        ),
    )
    require_purchase_order = models.BooleanField(
        default=False,
        help_text=(
            "Whether a purchase order must accompany a resource request for this "
            "offering before the proposal can be submitted. Defaults to the "
            "offering's require_purchase_order_upload, and stays under the call "
            "manager's control afterwards."
        ),
    )

    objects = managers.RequestedOfferingQuerySet.as_manager()

    def save(self, *args, **kwargs):
        # Seed from the offering the first time only. The offering flag gates
        # order *approval*, which happens after allocation; a call may need it
        # earlier (or not at all), so the call manager owns it from here on and
        # later offering changes must not silently rewrite the call's setting.
        if self._state.adding and not self.require_purchase_order:
            self.require_purchase_order = bool(
                self.offering.plugin_options.get("require_purchase_order_upload")
            )
        return super().save(*args, **kwargs)

    def requestable_components(self, priced=None):
        """The components of the offering a proposal requests an amount of,
        by type, each as billed under the call's plan: billed by the limit
        the order sets, and priced by the plan where there is one. A
        one-time component, or one the plan leaves out, is not requested.

        ``priced``, the component types the plan prices, saves looking them
        up when the caller already has them."""
        if self.plan_id:
            resolved = billing_mode.resolve_plan(self.plan)
            if priced is None:
                priced = set(
                    self.plan.components.values_list("component__type", flat=True)
                )
        else:
            resolved = billing_mode.resolve_offering(self.offering)
            priced = None
        return {
            component_type: effective
            for component_type, effective in resolved.components.items()
            if effective.billing_type == BillingTypes.LIMIT
            and (priced is None or component_type in priced)
        }


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


class CallOfferingComponent(core_models.UuidMixin, TimeStampedModel):
    """What a proposal may request of one component of a call offering.

    The only place a call bounds a request: the least and the most a proposal
    may ask for, or the fixed amount every proposal gets, and the amount the
    request form starts from. Amounts are in the component's measured unit
    and per its limit period -- the values an order's limits carry, which
    ``unit_factor`` relates to the backend's units only at the provider.

    The component is one the offering bills by the limit its order sets and
    its plan prices (``RequestedOffering.requestable_components``). The call's
    resource pools hang off these rows.
    """

    class Permissions:
        customer_path = "requested_offering__call__manager__customer"

    requested_offering = models.ForeignKey(
        RequestedOffering, on_delete=models.CASCADE, related_name="component_rules"
    )
    component_type = models.CharField(
        max_length=50,
        help_text=(
            "Type of the offering component: one billed by its limit that "
            "the call offering's plan prices."
        ),
    )
    min_per_proposal = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="The least a proposal may request. Empty: no minimum.",
    )
    max_per_proposal = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="The most a proposal may request. Empty: no maximum.",
    )
    is_fixed = models.BooleanField(
        default=False,
        help_text=(
            "Every proposal requests exactly default_amount; the applicant "
            "cannot change it."
        ),
    )
    default_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text=(
            "The amount a request starts from; with is_fixed, the amount every "
            "proposal requests."
        ),
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    limit_period = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text=(
            "The component's limit period when the call took the rule in. "
            "Pools count awards by it, so a provider changing the period "
            "later does not recount what the call already awarded."
        ),
    )

    requested_offering_id: int
    pools: models.Manager["CallResourcePool"]

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Call offering component")
        verbose_name_plural = _("Call offering components")
        constraints = [
            models.UniqueConstraint(
                fields=["requested_offering", "component_type"],
                name="proposal_unique_call_offering_component",
            )
        ]

    def __str__(self):
        return f"{self.requested_offering} - {self.component_type}"

    def clean(self):
        errors = {}
        low, high, default = (
            self.min_per_proposal,
            self.max_per_proposal,
            self.default_amount,
        )
        if self.is_fixed:
            if default is None:
                errors["default_amount"] = _(
                    "A fixed amount is the amount every proposal requests: set it."
                )
            elif any(v is not None and v != default for v in (low, high)):
                errors["is_fixed"] = _(
                    "A fixed amount is both the minimum and the maximum per "
                    "proposal: leave them empty or equal to it."
                )
        if low is not None and high is not None and low > high:
            errors["min_per_proposal"] = _(
                "The minimum cannot exceed the maximum per proposal."
            )
        elif default is not None and not self.is_fixed:
            if (low is not None and default < low) or (
                high is not None and default > high
            ):
                errors["default_amount"] = _(
                    "The default amount must lie between the minimum and the "
                    "maximum per proposal."
                )
        if self.requested_offering_id and self.component_type:
            effective = self.requested_offering.requestable_components().get(
                self.component_type
            )
            if effective is None:
                errors["component_type"] = _(
                    "The offering's plan does not price a component of this "
                    "type billed by its limit."
                )
            else:
                component = effective.component
                for name in ("min_per_proposal", "max_per_proposal", "default_amount"):
                    value = getattr(self, name)
                    if value is None or name in errors:
                        continue
                    if component.min_value is not None and value < component.min_value:
                        errors[name] = _(
                            "Below the least the component accepts (%(bound)s)."
                        ) % {"bound": component.min_value}
                    elif (
                        component.max_value is not None and value > component.max_value
                    ):
                        errors[name] = _(
                            "Above the most the component accepts (%(bound)s)."
                        ) % {"bound": component.max_value}
        if errors:
            raise DjangoValidationError(errors)

    def save(self, *args, **kwargs):
        # A fixed amount is the whole range a request may take.
        if self.is_fixed and self.default_amount is not None:
            self.min_per_proposal = self.max_per_proposal = self.default_amount
        if not self.limit_period:
            effective = self.requested_offering.requestable_components().get(
                self.component_type
            )
            self.limit_period = getattr(effective, "limit_period", "") or (
                self.requested_offering.offering.components.filter(
                    type=self.component_type
                )
                .values_list("limit_period", flat=True)
                .first()
                or ""
            )
        return super().save(*args, **kwargs)


class CallResourcePool(core_models.UuidMixin, TimeStampedModel):
    """How much of a call offering's component the call awards: over the
    whole call (no round), or over one of its rounds.

    A proposal draws on both the call-wide pool of a component and the pool
    of its own round, so "1M per call, at most 400k per cut-off" is a
    call-wide row and a row per round. The total is in the component's
    measured unit over the whole length of each award; how an award's
    amounts count against it is ``budgets``'s to say.
    """

    class Permissions:
        customer_path = (
            "offering_component__requested_offering__call__manager__customer"
        )

    offering_component = models.ForeignKey(
        CallOfferingComponent, on_delete=models.CASCADE, related_name="pools"
    )
    round = models.ForeignKey(
        "Round",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="resource_pools",
        help_text="The round the pool covers. Empty: the whole call.",
    )
    offered_total = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text=(
            "How much the call awards from the pool, in the component's unit "
            "over the whole length of each award."
        ),
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    offering_component_id: int
    round_id: int | None

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Call resource pool")
        verbose_name_plural = _("Call resource pools")
        constraints = [
            models.UniqueConstraint(
                fields=["offering_component", "round"],
                condition=Q(round__isnull=False),
                name="proposal_unique_round_resource_pool",
            ),
            models.UniqueConstraint(
                fields=["offering_component"],
                condition=Q(round__isnull=True),
                name="proposal_unique_call_resource_pool",
            ),
        ]

    def __str__(self):
        scope = self.round.name if self.round_id else "call"
        return f"{self.offering_component} - {scope}"

    def clean(self):
        if self.round_id and self.offering_component_id:
            call_id = self.offering_component.requested_offering.call_id
            if self.round.call_id != call_id:
                raise DjangoValidationError(
                    {"round": _("The round is not part of this call.")}
                )


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
    """Time-bounded submission and allocation-scheduling window within a call.

    Review and allocation *policy* (which steps run, how many reviewers, score
    thresholds, manual vs automatic transitions) lives on the per-call workflow
    step configuration (``CallWorkflowStep``), not here — the Round only carries
    scheduling (submission window, review duration, allocation timing).
    """

    class Meta:
        ordering = ["-created", "id"]

    class Statuses(RoundStatuses):
        pass

    review_duration_in_days = models.PositiveIntegerField(null=True, blank=True)
    allocation_date = models.DateTimeField(null=True, blank=True)
    start_time = models.DateTimeField()
    cutoff_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When submission closes. Empty: the round stays open until a call "
            "manager closes it."
        ),
    )
    call = models.ForeignKey(Call, on_delete=models.PROTECT)
    proposal_set: models.Manager["Proposal"]

    objects = managers.RoundQuerySet.as_manager()

    # Stored lifecycle after the cut-off; null until the cut-off is processed.
    # scheduled/open/ended stay derived from the times (``status``).
    lifecycle_state = models.CharField(
        max_length=20,
        choices=RoundLifecycleStates.CHOICES,
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "Where the round stands after its cut-off: evaluating, deciding, "
            "results_published or closed. Empty before the cut-off."
        ),
    )
    evaluation_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    deciding_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    results_published_at = models.DateTimeField(null=True, blank=True, editable=False)
    results_published_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    results_forced_reason = models.TextField(
        blank=True,
        editable=False,
        help_text=(
            "Why results were published while some proposals of the round "
            "still had no decision."
        ),
    )
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    undecided_at_round_completion = models.CharField(
        max_length=20,
        choices=UndecidedAtRoundCompletion.CHOICES,
        null=True,
        blank=True,
        help_text=(
            "Overrides the call's rule for proposals still without a decision "
            "when the round is completed. Empty: the call's rule applies."
        ),
    )
    # Adoption of the round's results by the body entitled to (e.g. a board).
    adopted_at = models.DateField(
        null=True, blank=True, help_text="When the round's results were adopted."
    )
    adoption_note = models.TextField(blank=True)
    adoption_document = models.FileField(
        upload_to="proposal_round_adoption", null=True, blank=True
    )

    class Permissions:
        customer_path = "call__manager__customer"
        list_permission = PermissionEnum.LIST_ROUNDS
        build_query = filter_rounds

    def __str__(self):
        return f"{self.call.name} | {self.start_time} - {self.cutoff_time or 'open'}"

    @property
    def is_open_ended(self) -> bool:
        """No cut-off: the round stays open until a call manager closes it."""
        return self.cutoff_time is None

    @property
    def name(self) -> str:
        start = self.start_time.strftime("%d.%m.%Y")
        if self.cutoff_time is None:
            return f"Round {start} – open"
        return f"Round {start}-{self.cutoff_time.strftime('%d.%m.%Y')}"

    @property
    def status(self) -> Literal["scheduled", "open", "ended"]:
        now = timezone.now()

        if self.start_time > now:
            return self.Statuses.SCHEDULED
        elif self.cutoff_time is not None and self.cutoff_time < now:
            return self.Statuses.ENDED
        else:
            return self.Statuses.OPEN

    # Where the round stands after its cut-off belongs to this round alone: a
    # duplicated or exported call starts its rounds without it.
    LIFECYCLE_FIELDS = (
        "lifecycle_state",
        "evaluation_started_at",
        "deciding_started_at",
        "results_published_at",
        "results_published_by",
        "results_forced_reason",
        "closed_at",
        "adopted_at",
        "adoption_note",
        "adoption_document",
    )

    def reset_lifecycle(self):
        for name in self.LIFECYCLE_FIELDS:
            setattr(self, name, self._meta.get_field(name).get_default())

    @property
    def undecided_at_completion_rule(self) -> str:
        """The rule for undecided proposals when the round is completed."""
        return (
            self.undecided_at_round_completion
            or self.call.undecided_at_round_completion
        )

    @property
    def withholds_results(self) -> bool:
        """Whether an allocation decision made now is held for publication."""
        return (
            self.call.publish_results == ResultsPublication.WITH_ROUND
            and self.lifecycle_state not in RoundLifecycleStates.PUBLISHED
        )

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
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Who attached the document.",
    )


def call_has_resource_templates_expression():
    """``Exists`` over the resource templates of a proposal's call."""
    return models.Exists(
        CallResourceTemplate.objects.filter(call=models.OuterRef("round__call"))
    )


def can_submit_annotations():
    """What ``Proposal.can_submit`` would otherwise query once per proposal."""
    return {
        "_has_proposal_manager": has_proposal_manager_expression(),
        "_call_has_resource_templates": call_has_resource_templates_expression(),
    }


def has_proposal_manager_expression():
    """``Exists`` over a proposal's active proposal manager grants.

    For querysets that serialise ``can_submit`` for many proposals at once;
    ``Proposal.has_proposal_manager`` reads the annotation when present.
    """
    return models.Exists(
        UserRole.objects.filter(
            # Matched by natural key rather than a ContentType lookup, so
            # building the queryset touches no database (schema generation).
            content_type__app_label="proposal",
            content_type__model="proposal",
            object_id=models.OuterRef("pk"),
            is_active=True,
            role__name=RoleEnum.PROPOSAL_MANAGER,
        )
    )


def reads_whole_proposal_q(user):
    """The branches of ``filter_proposals`` that read the whole proposal:
    every one but a provider's.

    A provider -- an offering manager of an offering the proposal requested
    or was awarded, or the person a per-offering slot is handed to -- reads
    the proposal for their own offering's part of it, and only that part
    (``permissions.offering_scope``). Whoever also reads it through any
    branch here reads all of it.
    """
    # The applicant team reads its proposal; the author only while still on
    # it. Nothing is granted through ``created_by``. An evaluator who joins
    # the team reads the proposal as one of the team, whatever the branches
    # below say: the evaluator views check team membership on their own.
    return (
        Q(pk__in=proposal_roles.applicant_team_proposal_ids(user))
        | Q(
            round__call__manager__customer__callmanagingorganisation__in=managers.get_connected_call_organizers(
                user
            )
        )
        # The call's managers read every proposal of the call, drafts
        # included; its panel members read its submitted ones, as the
        # committee. A call reviewer reads none through the call: the role is
        # the reviewer pool, and a reviewer reads only the proposals they hold
        # work on (the branches below).
        | Q(round__call__in=managers.get_connected_calls(user, RoleEnum.CALL_MANAGER))
        | (
            Q(
                round__call__in=managers.get_connected_calls(
                    user, RoleEnum.CALL_PANEL_MEMBER
                )
            )
            & ~Q(state=ProposalStates.DRAFT)
            # A conflict of interest standing with the proposal keeps a
            # panel member off it, as it does an evaluator role's holder.
            & ~models.Exists(
                ConflictOfInterest.objects.filter(
                    reviewer__user=user,
                    proposal_id=models.OuterRef("pk"),
                    status__in=proposal_roles.BLOCKING_COI_STATUSES,
                )
            )
        )
        # A reviewer reads the proposal they are reviewing through their review,
        # not through a call role: accepting a reviewer-pool invitation or an
        # assignment grants none.
        | Q(pk__in=managers.get_reviewed_proposals(user))
        # An assignment sent to the reviewer and not declined: they read the
        # proposal to decide whether to take it.
        | Q(pk__in=managers.get_assigned_proposals(user))
        # A contribution of their own to one of the proposal's steps that
        # still counts. An offering's slot is its team's: a provider's branch.
        | Q(pk__in=managers.get_contributed_proposals(user, per_offering=False))
        # An evaluator role on the proposal (a rapporteur, say) is held per
        # proposal, not through a call role.
        | Q(pk__in=proposal_roles.proposals_held_by(user))
        # A results reader of the managing organisation (CALL.VIEW_RESULTS)
        # reads the submitted proposals of a round once its results are out.
        | (
            Q(round__call__in=managers.get_results_readable_calls(user))
            & Q(round__lifecycle_state__in=RoundLifecycleStates.PUBLISHED)
            & ~Q(state=ProposalStates.DRAFT)
        )
    )


def filter_proposals(user):
    return (
        reads_whole_proposal_q(user)
        # Offering managers (technical reviewers) act on the technical_assessment
        # step, so they must be able to retrieve the non-draft proposals that
        # requested one of their accepted offerings (not every proposal on the
        # call).
        | Q(pk__in=managers.get_offering_manager_proposals(user))
        # A contribution to an offering's slot of one of the proposal's steps
        # that still counts.
        | Q(pk__in=managers.get_contributed_proposals(user, per_offering=True))
        # A per-offering slot handed to the user, once accepted and while
        # they may still take it over.
        | Q(pk__in=managers.get_delegated_proposals(user))
    )


def names_an_amount(offering, limits) -> bool:
    """Whether ``limits`` name an amount for something ``offering`` sells by it.

    The requestable components are the ones ``get_limit_components()`` selects
    -- limit components and one-time components bought by the month -- read off
    ``offering.components.all()`` so a prefetched list is used as is. An
    offering with none of them has no amount to name and always passes.
    Provisioning limits that name nothing creates a resource with no quota.
    """
    requestable = [
        component.type
        for component in offering.components.all()
        if component.billing_type == BillingTypes.LIMIT
        or (component.billing_type == BillingTypes.ONE_TIME and component.is_prepaid)
    ]
    if not requestable:
        return True
    limits = limits or {}
    return any(limits.get(component_type) for component_type in requestable)


REQUIRED_OPTIONS_MISSING_MESSAGE = _(
    "Required options are missing for the following offerings: %(offerings)s."
)
REQUIRED_OPTIONS_MISSING_FIELD_MESSAGE = _("Required options are missing: %(options)s.")


def _asks_for_an_answer(option) -> bool:
    """Whether an option can be left unanswered at all: required, without a
    default, typed in (not computed), and not a boolean, whose unticked box is
    an answer."""
    return (
        isinstance(option, dict)
        and bool(option.get("required"))
        and not option.get("default")
        and option.get("type") != "boolean"
        and option.get("type") not in NON_INPUT_FIELD_TYPES
    )


def _is_unanswered(value) -> bool:
    """No value, null, or text that is blank once trimmed: what an order
    refuses as empty. An empty list is an answer, as for an order."""
    return value is None or (isinstance(value, str) and not value.strip())


def missing_required_options(offering, attributes) -> list[str]:
    """Labels of the required options of ``offering`` that ``attributes`` leave empty.

    Requests, awards and templates are saved with required options optional
    (``utils.normalize_option_values``), so a draft may leave them for later.
    They are asked for before submission and the decision: ``allocate_proposal``
    creates the order without the serializer that would refuse it, so an option
    missing then reached the provider unchecked.

    A presence check over the options an order requires: one hidden by its
    ``visible_if`` rule, one with a default (the provider reads the default
    when the value is absent, as for an order) and a boolean are not required.
    The values themselves were checked when they were saved. Homeport's
    ``missingRequiredOptions`` applies the same rule, so the form and the
    submission agree.
    """
    options = (offering.options or {}).get("options") or {}
    asked = {name: o for name, o in options.items() if _asks_for_an_answer(o)}
    if not asked:
        return []
    values = attributes if isinstance(attributes, dict) else {}
    hidden = get_hidden_options(options, values)
    return [
        option.get("label") or name
        for name, option in asked.items()
        if name not in hidden and _is_unanswered(values.get(name))
    ]


def offerings_missing_options(items) -> list[str]:
    """``"Offering (Option, …)"`` for each item leaving a required option empty.

    ``items`` are requested or awarded resources: both carry the call's
    ``requested_offering`` and the ``attributes`` an order would take.
    """
    missing = set()
    for item in items:
        offering = item.requested_offering.offering
        labels = missing_required_options(offering, item.attributes)
        if labels:
            missing.add(f"{offering.name} ({', '.join(labels)})")
    return sorted(missing)


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
    project_summary = models.TextField(blank=True, max_length=PROJECT_SUMMARY_LENGTH)
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "When the proposal left draft. Null for proposals submitted before "
            "this field existed: their submission was never recorded and the "
            "only proxy, the first workflow step instance, was backfilled by "
            "migration for the oldest of them — so it is left empty rather "
            "than filled with a date that reads as fact."
        ),
    )

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

    panel = models.ForeignKey(
        CallPanel,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proposals",
        help_text=(
            "The call panel evaluating the proposal. Set from its science "
            "sub-domain when evaluation starts; changed by those managing the "
            "call and the completer of its panel assignment step. Never shown "
            "to the applicant team."
        ),
    )

    decision_held = models.BooleanField(
        default=False,
        editable=False,
        help_text=(
            "The allocation decision is recorded but held until the round's "
            "results are published; the applicant is not told yet."
        ),
    )

    # Workflow tracking
    current_step = models.ForeignKey(
        "ProposalWorkflowStepInstance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "The proposal's step in its workflow: the active one, or a "
            "completed one waiting to be moved on by hand."
        ),
    )
    awarded_resources_prefilled_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "When the awarded resources were first copied from the request. "
            "Set once: an award the call manager emptied stays empty rather "
            "than being refilled from the request."
        ),
    )
    requested_duration_months = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "The project length the applicant chose, in months, from the "
            "call's allowed_duration_months."
        ),
    )
    awarded_duration_months = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "The project length the allocation decision awards, in months: "
            "the requested one until the call manager picks another listed one."
        ),
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "When the accepted proposal's project was completed: its final "
            "report accepted. No run can be opened afterwards."
        ),
    )
    completed_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    # Note: checklist_completions relationship is automatically available via ChecklistCompletion.scope

    tracker = cast(FieldInstanceTracker, FieldTracker())
    current_step_id: int | None
    requestedresource_set: models.Manager["RequestedResource"]
    awarded_resources: models.Manager["AwardedResource"]
    review_set: models.Manager["Review"]
    workflow_step_instances: models.Manager["ProposalWorkflowStepInstance"]

    class Permissions:
        customer_path = "round__call__manager__customer"
        build_query = filter_proposals
        list_permission = PermissionEnum.LIST_PROPOSALS

    def __str__(self):
        return f"{self.name} | {self.round.start_time} - {self.round.cutoff_time or 'open'} | {self.round.call}"

    def user_can_read_checklist(self, user) -> bool:
        """Whoever reads the proposal's compliance answers opens the files
        attached to them (read by the checklist media access rule).

        As at the answers' endpoint: the proposal must be one the user can
        see at all, and the checklist rule then decides.
        """
        from waldur_core.structure.managers import filter_queryset_for_user
        from waldur_mastermind.proposal import permissions as proposal_permissions

        if user is None or user.is_anonymous:
            return False
        visible = filter_queryset_for_user(
            Proposal.objects.filter(pk=self.pk), user
        ).exists()
        return visible and proposal_permissions.reads_proposal_checklist(user, self)

    @classmethod
    def get_url_name(cls):
        return "proposal-proposal"

    class Meta:
        ordering = ["round__start_time", "id"]

    @property
    def workflow_step(self) -> str | None:
        """Key of the proposal's current call step, None outside the workflow."""
        return self.current_step.step if self.current_step_id else None

    @property
    def checklist_completion(self):
        """Get the checklist completion for this proposal."""
        if not self.round.call.compliance_checklist:
            return None

        return self.get_checklist_completion_for(self.round.call.compliance_checklist)

    def get_checklist_completion_for(self, checklist):
        """Return this proposal's completion for an arbitrary checklist, or None.

        ``ChecklistCompletion`` is keyed on ``(scope, checklist)``, so a proposal
        can hold a distinct completion per attached checklist (e.g. one per
        workflow step) alongside the call-level compliance completion.
        """
        if checklist is None:
            return None
        proposal_content_type = ContentType.objects.get_for_model(self)
        return checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=proposal_content_type,
            scope_object_id=self.id,
            checklist=checklist,
        ).first()

    def ensure_checklist_completion_for(self, checklist):
        """Get-or-create this proposal's completion for a checklist.

        Called when a workflow step with an attached checklist becomes active so
        the responsible role has a completion to answer against.
        """
        if checklist is None:
            return None
        proposal_content_type = ContentType.objects.get_for_model(self)
        completion, _ = checklist_models.ChecklistCompletion.objects.get_or_create(
            scope_content_type=proposal_content_type,
            scope_object_id=self.id,
            checklist=checklist,
        )
        return completion

    def offerings_missing_purchase_orders(self) -> list[str]:
        """Offerings whose call entry demands a purchase order and has none.

        Filtered in Python rather than in the query. ``can_submit`` reports this
        on every proposal a list serializes, and a ``.filter()`` on the relation
        ignores whatever the viewset prefetched — one query per row, which is
        what the annotations on ProposalViewSet.get_queryset exist to avoid.
        """
        return sorted(
            {
                requested_resource.requested_offering.offering.name
                for requested_resource in self.requestedresource_set.all()
                if requested_resource.requested_offering.require_purchase_order
                and not requested_resource.has_purchase_order
            }
        )

    def call_has_resource_templates(self) -> bool:
        annotated = getattr(self, "_call_has_resource_templates", None)
        if annotated is not None:
            return annotated
        return CallResourceTemplate.objects.filter(call_id=self.round.call_id).exists()

    def resource_template_error(self) -> str | None:
        """Why the requests break the call's template rules, or None."""
        # Creation enforces these too; this catches drafts from before the
        # rules, requests whose template was deleted, and calls that gained
        # templates after a draft was started.
        if not self.call_has_resource_templates():
            return None
        requested_resources = list(self.requestedresource_set.all())
        without_template = sorted(
            {
                requested_resource.requested_offering.offering.name
                for requested_resource in requested_resources
                if requested_resource.call_resource_template_id is None
            }
        )
        if without_template:
            return MISSING_RESOURCE_TEMPLATE_MESSAGE % {
                "offerings": ", ".join(without_template)
            }
        not_accepted = sorted(
            {
                requested_resource.requested_offering.offering.name
                for requested_resource in requested_resources
                if requested_resource.requested_offering.state
                != RequestedOfferingStates.ACCEPTED
            }
        )
        if not_accepted:
            return TEMPLATE_OFFERING_NOT_ACCEPTED_MESSAGE % {
                "offerings": ", ".join(not_accepted)
            }
        template_ids = [
            requested_resource.call_resource_template_id
            for requested_resource in requested_resources
        ]
        if len(template_ids) != len(set(template_ids)):
            return DUPLICATE_RESOURCE_TEMPLATE_MESSAGE
        single = (
            self.round.call.resource_template_selection_mode
            == ResourceTemplateSelectionModes.SINGLE
        )
        if single and not template_ids:
            return MISSING_SINGLE_TEMPLATE_MESSAGE
        if single and len(template_ids) > 1:
            return SEVERAL_TEMPLATES_MESSAGE
        # Lazily: utils imports this module.
        from waldur_mastermind.proposal import utils

        def matches_template(requested_resource):
            template = requested_resource.call_resource_template
            try:
                limits, attributes = utils.template_request_values(template)
            except ValidationError:
                # The template no longer passes its offering's options; this
                # runs wherever can_submit is shown, so it must not raise.
                return False
            return (
                requested_resource.limits,
                requested_resource.attributes,
                requested_resource.requested_offering_id,
            ) == (limits, attributes, template.requested_offering_id)

        outdated = sorted(
            {
                requested_resource.call_resource_template.name
                for requested_resource in requested_resources
                if not matches_template(requested_resource)
            }
        )
        if outdated:
            return OUTDATED_TEMPLATE_REQUEST_MESSAGE % {
                "templates": ", ".join(outdated)
            }
        return None

    def offerings_missing_requested_amounts(self) -> list[str]:
        """Offerings asked for without naming any amount.

        Offerings with nothing to ask for (no limit or prepaid component) are
        exempt: there is no amount to name in the first place.
        """
        return sorted(
            {
                requested_resource.requested_offering.offering.name
                for requested_resource in self.requestedresource_set.all()
                if not names_an_amount(
                    requested_resource.requested_offering.offering,
                    requested_resource.limits,
                )
            }
        )

    def offerings_missing_required_options(self) -> list[str]:
        """Requested offerings whose required options are left empty.

        Read off ``requestedresource_set.all()``, so the prefetch that
        ``can_submit`` relies on in lists is used as is.
        """
        return offerings_missing_options(self.requestedresource_set.all())

    def offerings_missing_awarded_options(self) -> list[str]:
        """The award's counterpart of :meth:`offerings_missing_required_options`.

        Over the items allocation provisions, as
        :meth:`offerings_missing_awarded_amounts`: a call manager may change
        an award's options, and must not drop a required one.
        """
        return offerings_missing_options(
            self.awarded_resources.filter(
                requested_offering__state=RequestedOfferingStates.ACCEPTED
            ).select_related("requested_offering__offering")
        )

    def offerings_missing_awarded_amounts(self) -> list[str]:
        """Offerings the award grants without naming any amount.

        The award's counterpart of :meth:`offerings_missing_requested_amounts`,
        over the items allocation provisions: those on offerings the provider
        still accepts. Anything else is skipped at allocation anyway.
        """
        return sorted(
            {
                awarded.requested_offering.offering.name
                for awarded in self.awarded_resources.filter(
                    requested_offering__state=RequestedOfferingStates.ACCEPTED
                )
                .select_related("requested_offering__offering")
                .prefetch_related("requested_offering__offering__components")
                if not names_an_amount(
                    awarded.requested_offering.offering, awarded.limits
                )
            }
        )

    def has_proposal_manager(self) -> bool:
        annotated = getattr(self, "_has_proposal_manager", None)
        if annotated is not None:
            return annotated
        return UserRole.objects.filter(
            scope=self, is_active=True, role__name=RoleEnum.PROPOSAL_MANAGER
        ).exists()

    def can_submit(self):
        """Whether the proposal may leave draft, and why not when it may not.

        Reports the same conditions the submit action enforces, so the form can
        say what is missing instead of letting the applicant find out from a
        rejected request -- among them the required questions of the call's
        compliance checklist.
        """
        if self.round.call.state != CallStates.ACTIVE:
            return False, CALL_NOT_ACTIVE_MESSAGE

        # Before amounts: fixing amounts or a purchase order on a request the
        # template rules refuse anyway would be wasted work.
        template_error = self.resource_template_error()
        if template_error:
            return False, template_error

        missing_amounts = self.offerings_missing_requested_amounts()
        if missing_amounts:
            return False, _(
                "Requested amounts are missing for the following offerings: "
                "%(offerings)s."
            ) % {"offerings": ", ".join(missing_amounts)}

        missing_options = self.offerings_missing_required_options()
        if missing_options:
            return False, REQUIRED_OPTIONS_MISSING_MESSAGE % {
                "offerings": ", ".join(missing_options)
            }

        missing_orders = self.offerings_missing_purchase_orders()
        if missing_orders:
            return False, _(
                "A purchase order is required for the following offerings: "
                "%(offerings)s."
            ) % {"offerings": ", ".join(missing_orders)}

        if not self.has_proposal_manager():
            return False, MISSING_PROPOSAL_MANAGER_MESSAGE

        duration_error = self.requested_duration_error()
        if duration_error:
            return False, duration_error

        unanswered = self.unanswered_compliance_questions()
        if unanswered:
            return False, unanswered_compliance_message(unanswered)

        return True, None

    def can_submit_changes(self, parts):
        """Whether the changes asked of the applicant may be submitted, and
        why not when they may not.

        Only the ``parts`` the open requests for changes unlock are checked
        (``ChangeRequestParts``): the rest is what was submitted, and the
        applicant cannot change it now. Checking it too would refuse the
        changes for something only the call team can mend -- a compliance
        question added after submission, a template edited since -- and leave
        the request open with nothing the applicant can do about it.
        """
        if ChangeRequestParts.RESOURCES in parts:
            template_error = self.resource_template_error()
            if template_error:
                return False, template_error
            missing_amounts = self.offerings_missing_requested_amounts()
            if missing_amounts:
                return False, _(
                    "Requested amounts are missing for the following offerings: "
                    "%(offerings)s."
                ) % {"offerings": ", ".join(missing_amounts)}
            missing_options = self.offerings_missing_required_options()
            if missing_options:
                return False, REQUIRED_OPTIONS_MISSING_MESSAGE % {
                    "offerings": ", ".join(missing_options)
                }
            missing_orders = self.offerings_missing_purchase_orders()
            if missing_orders:
                return False, _(
                    "A purchase order is required for the following offerings: "
                    "%(offerings)s."
                ) % {"offerings": ", ".join(missing_orders)}

        if ChangeRequestParts.TEAM in parts and not self.has_proposal_manager():
            return False, MISSING_PROPOSAL_MANAGER_MESSAGE

        if ChangeRequestParts.field(ChangeRequestParts.DURATION_FIELD) in parts:
            duration_error = self.requested_duration_error()
            if duration_error:
                return False, duration_error

        if ChangeRequestParts.COMPLIANCE in parts:
            unanswered = self.unanswered_compliance_questions()
            if unanswered:
                return False, unanswered_compliance_message(unanswered)

        return True, None

    def requested_duration_error(self):
        """Why the chosen duration cannot be submitted, or None when it can.

        A call that offers a list of durations needs one of them chosen; a
        call that offers none ignores the choice.
        """
        allowed = self.round.call.allowed_duration_months
        if not allowed:
            return None
        if self.requested_duration_months is None:
            return _("Choose how long the project should run.")
        if self.requested_duration_months not in allowed:
            return duration_options_message(allowed)
        return None

    def unanswered_compliance_questions(self) -> int:
        """How many required questions of the call's compliance checklist the
        proposal has left unanswered: all of them before it answers any."""
        checklist = self.round.call.compliance_checklist
        if not checklist:
            return 0
        completion = self.get_checklist_completion_for(checklist)
        if completion is None:
            return checklist.questions.filter(required=True).count()
        return completion.get_unanswered_required_questions().count()

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


class RequestedResource(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    """Specific resource requests within proposals, linking to marketplace resources with attributes and limits configuration."""

    class Meta:
        ordering = ["-created", "id"]

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
    # Collected here rather than at order approval so the reviewer sees the
    # authorisation alongside the amounts, and so allocate_proposal can hand it
    # to the order it creates instead of asking the applicant a second time.
    purchase_order_reference = models.CharField(max_length=255, blank=True)
    attachment = models.FileField(
        upload_to="proposal_requested_resource_attachments",
        blank=True,
        null=True,
    )
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

    requested_offering_id: int
    awarded_resources: models.Manager["AwardedResource"]

    @property
    def purchase_order_required(self) -> bool:
        return self.requested_offering.require_purchase_order

    @property
    def has_purchase_order(self) -> bool:
        """Either half satisfies the requirement.

        Some providers want the document, others only need the reference from
        the customer's finance system; demanding both would block the second
        group for no gain.
        """
        return bool(self.attachment) or bool(self.purchase_order_reference)


def awarded_order_refused():
    """For an AwardedResource queryset: whether the provider refused (or the
    consumer called off) the order that was to create the item's resource.
    Such an item provisioned nothing; one whose resource later ended -- the
    project ran out, the provider terminated it -- did, and still counts."""
    return Exists(
        marketplace_models.Order.objects.filter(
            resource=OuterRef("resource"),
            type=marketplace_enums.OrderTypes.CREATE,
            state__in=(
                marketplace_enums.OrderStates.REJECTED,
                marketplace_enums.OrderStates.CANCELED,
            ),
        )
    )


class AwardedResource(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    """What the allocation decision grants, which may differ from the request.

    Prefilled from the proposal's requested resources when the allocation
    decision starts, then edited by the call manager while that step is
    active: amounts changed, an item moved to another accepted offering of the
    call, items added or removed. Allocation provisions these rows; the
    requested resources stay as the applicant submitted them.
    """

    class Meta:
        ordering = ["created", "id"]

    class Permissions:
        project_path = "proposal__project"

    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="awarded_resources"
    )
    requested_resource = models.ForeignKey(
        RequestedResource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="awarded_resources",
        help_text=(
            "The request this item was prefilled from; empty for an item the "
            "call manager added."
        ),
    )
    requested_offering = models.ForeignKey(
        RequestedOffering,
        related_name="+",
        on_delete=models.PROTECT,
        help_text="The call's offering the item is awarded on.",
    )
    plan = models.ForeignKey(
        marketplace_models.Plan,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=(
            "The plan the call manager chose for the item. Empty for an item "
            "that follows the plan of the call offering it is awarded on."
        ),
    )
    attributes = models.JSONField(blank=True, default=dict)
    limits = models.JSONField(blank=True, default=dict)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    resource = models.ForeignKey(
        marketplace_models.Resource,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The resource allocation provisioned for this item.",
    )
    modified_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Who last changed the item. Empty for one nobody changed since "
            "it was copied from the request or added."
        ),
    )
    provision_error = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Why allocation could not order the item -- the offering stopped "
            "taking orders, its plan was archived, its limits no longer fit. "
            "Empty once it is provisioned."
        ),
    )

    requested_offering_id: int

    @property
    def effective_plan(self) -> marketplace_models.Plan | None:
        """The plan allocation provisions the item on.

        The one the call manager chose, or else the call offering's plan as it
        is at the time of asking -- so an item nobody changed the plan of
        provisions on the call's plan at allocation, as a request always has.
        """
        return self.plan or self.requested_offering.plan

    def __str__(self):
        return f"{self.proposal.name} - {self.requested_offering.offering.name}"


class ProposalRun(TimeStampedModel, core_models.UuidMixin):
    """One walk of an accepted proposal through a post-award phase.

    An extension run for each extension the applicant asks for, a closing
    run for the final report. Its steps are the phase's steps of the call,
    instantiated afresh for the run, so each has its own forms and
    contributions. A run never allocates nor rejects: the proposal stays
    accepted whatever its outcome. At most one run of a proposal is open.

    An extension run carries what was asked and what was granted: the new
    end date of the project and its resources, and new limits per resource
    (``{<resource uuid>: {<component type>: <amount>}}``).
    """

    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="runs"
    )
    phase = models.CharField(
        max_length=16,
        choices=enums.WorkflowPhases.CHOICES,
        help_text="The post-award phase the run walks: extension or closing.",
    )
    state = models.CharField(
        max_length=16,
        choices=enums.ProposalRunStates.CHOICES,
        default=enums.ProposalRunStates.OPEN,
        db_index=True,
    )
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Who opened the run; empty when the system opened it.",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    requested_end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Extension: the end date the applicant asks for.",
    )
    requested_limits = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extension: the limits the applicant asks for, per resource.",
    )
    granted_end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Extension: the end date the decision grants.",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Extension: who last set what the decision grants.",
    )
    granted_limits = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extension: the limits the decision grants, per resource.",
    )
    applied_changes = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Extension: what the approval changed -- the end date requests "
            "recorded and the orders created -- and what it left alone, and why."
        ),
    )

    proposal_id: int
    step_instances: models.Manager["ProposalWorkflowStepInstance"]

    class Meta:
        ordering = ["created", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal"],
                condition=Q(state=enums.ProposalRunStates.OPEN),
                name="proposal_unique_open_run",
            ),
        ]

    def __str__(self):
        return f"{self.proposal.name} — {self.phase} [{self.state}]"

    @property
    def is_open(self) -> bool:
        return self.state == enums.ProposalRunStates.OPEN


class ProposalWorkflowStepInstance(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """Tracks the status and outcome of each workflow step for a specific proposal.

    One instance per proposal per call step, created when the proposal is
    submitted: pending for an enabled step, skipped for a disabled one.
    """

    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        related_name="workflow_step_instances",
    )
    call_step = models.ForeignKey(
        CallWorkflowStep,
        # A step a proposal has walked is history: it is disabled, not deleted.
        # Deleting the whole call still takes its proposals' steps with it.
        on_delete=models.RESTRICT,
        related_name="proposal_instances",
    )
    step = models.CharField(
        max_length=64,
        help_text="Key of the call step, copied from it.",
    )
    # Copied from the call step when the proposal's path is fixed, so that
    # reordering the call's steps, or moving its allocation decision, only
    # affects proposals submitted afterwards.
    order = models.PositiveIntegerField(
        help_text="Position of the step in this proposal's path.",
    )
    is_allocation_decision = models.BooleanField(
        default=False,
        help_text="Whether this step settles the proposal's award.",
    )
    stage = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "The stage the step runs in on this proposal: the order of the "
            "stage's first step. Steps of one stage run at the same time. "
            "Empty on proposals submitted before stages, whose every step is "
            "a stage of its own."
        ),
    )
    run_condition = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "The call step's run condition when the proposal was submitted; "
            "empty to run on every proposal."
        ),
    )
    run = models.ForeignKey(
        ProposalRun,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="step_instances",
        help_text=(
            "The post-award run the step belongs to; empty for a step of the "
            "proposal's evaluation."
        ),
    )

    call_step_id: int
    run_id: int | None
    status = models.CharField(
        max_length=20,
        choices=enums.WorkflowStepInstanceStatuses.CHOICES,
        default=enums.WorkflowStepInstanceStatuses.PENDING,
    )
    outcome = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Step-specific outcome (e.g., eligible, feasible, approved).",
    )
    outcome_reason = models.TextField(
        blank=True,
        help_text="Explanation for the outcome (e.g., rejection reason).",
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this step became active.",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    deadline = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Computed from started_at + step duration_in_days.",
    )
    sent_notifications = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Ledger of notification-rule events already dispatched for this "
            'instance ("<trigger>" or "deadline_approaching:<days>"), so '
            "a reminder is never sent twice."
        ),
    )
    # Copied from the call step when the proposal's path is fixed, so that
    # changing a step's inputs only affects proposals submitted afterwards.
    input_steps = models.ManyToManyField(
        CallWorkflowStep,
        blank=True,
        related_name="+",
        help_text="The call steps whose outputs this step reads on the proposal.",
    )
    released_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the step's report was released to the applicant.",
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Who released the step's report to the applicant.",
    )
    released_text = models.TextField(
        blank=True,
        help_text=(
            "The text the applicant reads in place of the step's report, as "
            "edited when it was released; the report itself when empty."
        ),
    )
    feedback_withheld = models.BooleanField(
        default=False,
        help_text=(
            "The outcome reason is feedback on a step whose report is "
            "released to the applicant; the applicant reads it only once the "
            "report is released."
        ),
    )
    internal_notes = models.TextField(
        blank=True,
        help_text=(
            "Notes captured by the call-management team when completing or "
            "rejecting the step. Never shown to the applicant. Visible in API "
            "responses to staff and to any user who holds an active role on "
            "the proposal's call (see permissions.user_can_view_internal_notes)."
        ),
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Proposal workflow step")
        verbose_name_plural = _("Proposal workflow steps")
        # Several steps of one stage are active at once; every path that
        # changes a proposal's steps locks the proposal row first, which is
        # what serialises them.
        # A step runs once in the evaluation, and once in each post-award run.
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "call_step"],
                condition=Q(run__isnull=True),
                name="proposal_unique_step_instance_per_call_step",
            ),
            models.UniqueConstraint(
                fields=["run", "call_step"],
                condition=Q(run__isnull=False),
                name="proposal_unique_step_instance_per_run_step",
            ),
        ]

    def __str__(self):
        return f"{self.proposal.slug} — {self.step} [{self.status}]"

    @property
    def stage_key(self) -> int:
        """The stage the step runs in; its own order when it has none."""
        return self.order if self.stage is None else self.stage

    @property
    def responds_to_inputs(self) -> bool:
        """Whether this is a response step on the proposal: an applicant step
        whose outcome is a submission and that reads earlier steps."""
        call_step = self.call_step
        return (
            call_step.kind == enums.StepKinds.APPLICANT_ACTION
            and call_step.outcome_set == enums.OutcomeSets.SUBMITTED
            and self.input_steps.exists()
        )

    def save(self, *args, **kwargs):
        # ``step`` mirrors the call step's key. A caller that names the step
        # by key alone gets it resolved within the proposal's call.
        if self.call_step_id is None and self.step:
            self.call_step = CallWorkflowStep.objects.get(
                call_id=self.proposal.round.call_id, key=self.step
            )
        elif self.call_step_id is not None:
            self.step = self.call_step.key
        if self._state.adding:
            if self.order is None:
                self.order = self.call_step.order
            self.is_allocation_decision = self.call_step.is_allocation_decision
        super().save(*args, **kwargs)


class ProposalRoleAssignment(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """A person holding one of the call's evaluator roles on a proposal.

    Revoking keeps the row, with who revoked it and when; only rows not
    revoked count. Read through ``proposal_roles``, the one place that turns
    these rows into holders, never directly.
    """

    proposal = models.ForeignKey(
        "Proposal",
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.ForeignKey(
        CallProposalRole,
        # Deleting the call takes its roles and every proposal's assignments;
        # a role with assignments cannot be deleted on its own.
        on_delete=models.RESTRICT,
        related_name="assignments",
    )
    # Protected, as a review is: who evaluated the proposal stays known.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    is_lead = models.BooleanField(default=False)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    assigned_in = models.ForeignKey(
        "ProposalWorkflowStepInstance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="role_assignments",
        help_text="The assignment step the holder was picked in, if any.",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    revoked_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Why the role was taken away, when not by hand.",
    )
    coi_confirmation_required = models.BooleanField(
        default=False,
        help_text=(
            "The holder confirms having no conflict of interest before gaining "
            "access to the proposal."
        ),
    )
    coi_confirmed_at = models.DateTimeField(null=True, blank=True)
    coi_declaration_text = models.TextField(
        blank=True,
        help_text=(
            "The declaration the holder confirmed, as worded when they did; "
            "empty for the platform's standard one."
        ),
    )
    coi_declaration_only = models.BooleanField(
        default=False,
        help_text=(
            "The holder had no reviewer profile, so no recorded conflict could "
            "be checked: their own declaration is all there is."
        ),
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Evaluator role assignment")
        verbose_name_plural = _("Evaluator role assignments")
        constraints = [
            # One evaluator role per person per proposal.
            models.UniqueConstraint(
                fields=["proposal", "user"],
                condition=Q(revoked_at__isnull=True),
                name="proposal_unique_active_role_assignment",
            ),
            models.UniqueConstraint(
                fields=["proposal", "role"],
                condition=Q(revoked_at__isnull=True, is_lead=True),
                name="proposal_unique_active_role_lead",
            ),
        ]

    def __str__(self):
        return f"{self.proposal.slug} — {self.role.name}: {self.user}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def awaits_coi_confirmation(self) -> bool:
        return (
            self.revoked_at is None
            and self.coi_confirmation_required
            and self.coi_confirmed_at is None
        )


def filter_reviews(user):
    """The reviews ``user`` reads, as a query for object lookups by URL, which
    the events API resolves its ``scope`` through."""
    return Q(pk__in=managers.filter_reviews_for_user(user).values("pk"))


class Review(
    TimeStampedModel,
    core_models.UuidMixin,
    LoggableMixin,
):
    """Peer review system with detailed scoring, public/private comments, and field-specific feedback for all proposal aspects."""

    class Meta:
        ordering = ["-created", "id"]

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
        build_query = filter_reviews

    proposal = models.ForeignKey(Proposal, on_delete=models.PROTECT)
    state = models.CharField(
        default=States.IN_REVIEW,  # Changed from CREATED
        choices=States.CHOICES,
        db_index=True,
        max_length=10,
    )
    summary_score = models.PositiveSmallIntegerField(
        blank=True,
        default=0,
        validators=[MaxValueValidator(enums.REVIEW_SCORE_MAX)],
        help_text=(
            "The review's overall score, 1-5 (0 until rated). When the step "
            "has criteria, it is their weighted average."
        ),
    )
    criteria_scores = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "The reviewer's score per criterion of the step the review is "
            "given in, 1-5, keyed by criterion UUID."
        ),
    )
    rejection_cause = models.CharField(
        max_length=10,
        blank=True,
        choices=enums.ReviewRejectionCauses.CHOICES,
        help_text=(
            "Why a rejected review was closed: declined by the reviewer, "
            "expired at its deadline, or withdrawn when the reviewer joined "
            "the proposal team. Empty on reviews rejected before this was "
            "recorded."
        ),
    )
    summary_public_comment = models.TextField(blank=True)
    summary_private_comment = models.TextField(blank=True)
    comment_project_title = models.CharField(max_length=255, null=True, blank=True)
    comment_project_summary = models.CharField(max_length=255, null=True, blank=True)
    comment_project_description = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_supporting_documentation = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_resource_requests = models.CharField(max_length=255, null=True, blank=True)
    comment_team = models.CharField(max_length=255, null=True, blank=True)

    # Protected: a review is part of the proposal's record. Deactivate a
    # reviewer instead of deleting them.
    reviewer = models.ForeignKey[core_models.User](
        to=settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    # Reviewer's per-proposal attestation of absence of conflict of interest.
    # Enforced at review submission only when the call has an enabled workflow
    # step with ``requires_coi_confirmation`` set. This is the "no conflict"
    # counterpart to the ConflictOfInterest model (which records conflicts that
    # *do* exist); keeping it here avoids polluting that manager-resolution queue.
    coi_confirmed = models.BooleanField(
        default=False,
        help_text="Reviewer confirmed absence of conflict of interest with this proposal.",
    )
    coi_confirmed_at = models.DateTimeField(null=True, blank=True)
    coi_declaration_text = models.TextField(
        blank=True,
        help_text=(
            "The declaration the reviewer confirmed, as worded when they did; "
            "empty for the platform's standard one."
        ),
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "When the reviewer submitted the review. Empty while it is not "
            "submitted, and for reviews submitted before this was recorded."
        ),
    )

    objects = managers.ReviewQuerySet.as_manager()
    tracker = cast(FieldInstanceTracker, FieldTracker())

    @classmethod
    def get_url_name(cls):
        return "proposal-review"

    def get_log_fields(self):
        # Never the reviewer: an event naming them would reach whoever reads
        # the feeds it is filed on.
        return ("uuid",)

    @classmethod
    def get_permitted_objects(cls, user):
        return managers.filter_reviews_for_user(user)

    @property
    def step_deadline(self) -> datetime | None:
        """The deadline of the workflow step the review contributes to: the
        round's due date for the step when one is set, else the step's
        duration from when it started. None before the step has one."""
        try:
            contribution = self.contribution
        except ObjectDoesNotExist:
            return None
        if contribution is None or contribution.proposal_step_id is None:
            return None
        return contribution.proposal_step.deadline

    @property
    def review_end_date(self) -> datetime | None:
        """When the review is due, or None when nothing sets a deadline.

        The deadline of the step the review contributes to. A review whose
        step has none -- not started yet, or with no duration -- falls back
        to the round's review duration from its creation, as before steps had
        deadlines. A review created by accepting an assignment is also due no
        earlier than its assignment batch's deadline, so extending the batch
        deadline moves the deadline of the reviews accepted from it.
        """
        end_date = self.step_deadline
        if end_date is None:
            duration = self.proposal.round.review_duration_in_days
            if not duration:
                return None
            end_date = self.created + timedelta(days=duration)
        try:
            batch_expires_at = self.assignment_item.batch.expires_at
        except ObjectDoesNotExist:
            return end_date
        if batch_expires_at and batch_expires_at > end_date:
            return batch_expires_at
        return end_date


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

    # The operator's say over who reviews (see ReviewerStandings): set through
    # the staff reviewer registry only, so every change is logged.
    standing = models.CharField(
        max_length=20,
        choices=enums.ReviewerStandings.CHOICES,
        default=enums.ReviewerStandings.ACTIVE,
        db_index=True,
        help_text=_(
            "Only an active reviewer is selected for, invited to or offered "
            "new review work."
        ),
    )
    standing_reason = models.TextField(blank=True)
    standing_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    standing_changed_at = models.DateTimeField(null=True, blank=True)

    tracker = cast(FieldInstanceTracker, FieldTracker())
    objects = managers.ReviewerProfileQuerySet.as_manager()

    class Permissions:
        customer_path = None  # Reviewer profiles are user-scoped, not customer-scoped

    class Meta:
        # The reviewer registry pages through profiles.
        ordering = ["created", "id"]
        verbose_name = _("Reviewer profile")
        verbose_name_plural = _("Reviewer profiles")

    def __str__(self):
        return f"ReviewerProfile: {self.user.full_name}"

    @classmethod
    def get_url_name(cls):
        return "reviewer-profile"

    @property
    def is_eligible(self) -> bool:
        return self.standing == enums.ReviewerStandings.ACTIVE and self.user.is_active


class CallReviewerEligibility(core_models.UserDetailsMatchMixin, TimeStampedModel):
    """Who may review for a call: email patterns, affiliations, identity
    sources and assurance, as for applicants, but kept apart from the call's
    own (applicant) restrictions -- the people who review a call are rarely
    held to the rules of those who apply to it.

    Empty, it admits every reviewer. Read through validate_user_restrictions
    and evaluate_for_user, as any UserDetailsMatchMixin scope; it carries no
    customer, so no organisation's rules are folded in.
    """

    call = models.OneToOneField(
        "Call", on_delete=models.CASCADE, related_name="reviewer_eligibility"
    )

    class Meta:
        verbose_name = _("Call reviewer eligibility")

    def __str__(self):
        return f"Reviewer eligibility of {self.call}"


class ReviewerPublicationRequest(
    core_models.UuidMixin, core_mixins.ReviewMixin, LoggableMixin
):
    """A reviewer's request to publish their profile, while the operator
    approves publication (REVIEWER_PUBLICATION_REQUIRES_APPROVAL).

    Approving publishes the profile; rejecting leaves it unpublished, with the
    operator's comment for the reviewer to read. One request is pending per
    profile at a time.
    """

    profile = models.ForeignKey(
        ReviewerProfile,
        on_delete=models.CASCADE,
        related_name="publication_requests",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created", "id"]
        verbose_name = _("Reviewer publication request")
        constraints = [
            models.UniqueConstraint(
                fields=["profile"],
                condition=models.Q(state=ReviewStates.PENDING),
                name="unique_pending_reviewer_publication_request",
            )
        ]

    class Permissions:
        customer_path = None

    def get_log_fields(self):
        return ("uuid", "state")

    def approve(self, user, comment=None):
        super().approve(user, comment)
        profile = self.profile
        profile.is_published = True
        profile.published_at = timezone.now()
        profile.save(update_fields=["is_published", "published_at"])

    def __str__(self):
        return f"Publication request of {self.profile}"


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
        ordering = ["-start_date", "id"]
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
        ordering = ["-created", "id"]
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
        ordering = ["-publication_year", "id"]
        indexes = [
            models.Index(fields=["doi"]),
            models.Index(fields=["publication_year"]),
        ]

    def __str__(self):
        return f"{self.title[:50]}... ({self.publication_year})"


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

    # The three type-handling rules must stay mutually exclusive and hold only
    # known type codes. Both the API serializer and the bulk importer write
    # these lists, so the invariant lives with the model rather than in either
    # caller.
    RULE_FIELDS = (
        "recusal_required_types",
        "management_allowed_types",
        "disclosure_only_types",
    )

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

    @classmethod
    def find_rule_overlaps(cls, rules) -> dict[str, set[str]]:
        """Map each COI type assigned to more than one rule to those rule names."""
        assignments = {}
        for field in cls.RULE_FIELDS:
            for coi_type in rules.get(field) or []:
                assignments.setdefault(coi_type, set()).add(field)
        return {
            coi_type: fields
            for coi_type, fields in assignments.items()
            if len(fields) > 1
        }

    @classmethod
    def find_unknown_types(cls, rules) -> dict[str, list[str]]:
        """Map each rule name to the type codes it holds that are not valid COI types."""
        known = {choice[0] for choice in COITypes.CHOICES}
        problems = {}
        for field in cls.RULE_FIELDS:
            unknown = [
                coi_type for coi_type in rules.get(field) or [] if coi_type not in known
            ]
            if unknown:
                problems[field] = unknown
        return problems


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
        ordering = ["-detected_at", "id"]
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
        ordering = ["-certification_date", "id"]
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


def generate_invitation_token():
    # A field default rather than save(), so bulk_create gets one too
    return secrets.token_urlsafe(48)


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
    # Not maintained and not read: the open assignment count is derived by
    # ``CallReviewerPoolQuerySet.with_open_assignments`` instead.
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
        default=generate_invitation_token,
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

    objects = managers.CallReviewerPoolQuerySet.as_manager()
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "call__manager__customer"
        build_query = filter_call_reviewer_pool

    class Meta:
        verbose_name = _("Call reviewer pool member")
        verbose_name_plural = _("Call reviewer pool members")
        ordering = ["-invited_at", "id"]
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

    DEFAULT_INVITATION_EXPIRATION_DAYS = 7

    @classmethod
    def get_invitation_expires_at(cls, call) -> datetime:
        """When a pool invitation sent now for this call stops being answerable.

        Pool invitations share the call's assignment expiration setting, so a
        manager configures one response window for both stages of reviewer
        recruitment.
        """
        try:
            days = call.assignment_configuration.assignment_expiration_days
        except CallAssignmentConfiguration.DoesNotExist:
            days = cls.DEFAULT_INVITATION_EXPIRATION_DAYS
        return timezone.now() + timedelta(days=days)

    @property
    def is_invitation_expired(self) -> bool:
        if self.invitation_status == ReviewerPoolInvitationStatuses.EXPIRED:
            return True
        return bool(
            self.invitation_status == ReviewerPoolInvitationStatuses.PENDING
            and self.invitation_expires_at
            and self.invitation_expires_at < timezone.now()
        )

    # Who an entry is addressed to. One bound to a reviewer profile belongs to
    # that reviewer's account alone. Otherwise it belongs to the account it was
    # sent to and to any account with the invited address: addresses are not
    # unique to an account, and the invited one was picked by address, so the
    # same person's other account (an SSO one, say) answers it too. Addresses
    # are compared case-insensitively, the way ``__iexact`` compares them, and
    # never when either side is empty.

    def is_addressed_to(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        if self.reviewer_id:
            return self.reviewer.user_id == user.id
        if self.invited_user_id and self.invited_user_id == user.id:
            return True
        return bool(
            user.email
            and self.invited_email
            and self.invited_email.lower() == user.email.lower()
        )

    @staticmethod
    def addressed_to(user) -> Q:
        """Query matching the entries ``is_addressed_to`` holds true for."""
        query = Q(reviewer__user=user) | Q(reviewer__isnull=True, invited_user=user)
        if user.email:
            query |= Q(reviewer__isnull=True, invited_email__iexact=user.email)
        return query

    @property
    def effective_invitation_status(self) -> str:
        """The invitation status with the deadline applied.

        A pending invitation past its deadline is expired, whether or not the
        periodic expiry task has stored that yet.
        """
        if self.is_invitation_expired:
            return ReviewerPoolInvitationStatuses.EXPIRED
        return self.invitation_status

    def get_effective_invitation_status_display(self) -> str:
        return str(
            dict(ReviewerPoolInvitationStatuses.CHOICES)[
                self.effective_invitation_status
            ]
        )

    @property
    def invitee_name(self) -> str:
        if self.reviewer:
            return self.reviewer.user.full_name
        if self.invited_user:
            return self.invited_user.full_name
        return self.invited_email

    def get_open_assignments(self) -> int:
        """Number of assignments currently occupying this reviewer's workload."""
        annotated = getattr(self, "open_assignments", None)
        if annotated is not None:
            return annotated
        return (
            type(self)
            .objects.filter(pk=self.pk)
            .with_open_assignments()
            .values_list("open_assignments", flat=True)
            .get()
        )


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
        ordering = ["-affinity_score", "id"]
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
        ordering = ["-created", "id"]

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
        ordering = ["proposal", "rank", "id"]

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
        ordering = ["-submitted_at", "id"]

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
        ordering = ["-created", "id"]
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
        # Reviewer's own batches, once they have been sent
        Q(
            reviewer_pool_entry__reviewer__user=user,
            status__in=AssignmentBatchStatuses.SENT_TO_REVIEWER,
        )
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

    call_step = models.ForeignKey(
        CallWorkflowStep,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignment_batches",
        help_text=_(
            "The step the reviews accepted from this batch contribute to. "
            "The call's review step when empty."
        ),
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

    items: models.Manager["AssignmentItem"]

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Permissions:
        customer_path = "call__manager__customer"
        build_query = filter_assignment_batches

    class Meta:
        verbose_name = _("Assignment batch")
        verbose_name_plural = _("Assignment batches")
        ordering = ["-created", "id"]
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
        if self._state.adding and self.call_step_id is None:
            self.call_step = CallWorkflowStep.review_step_for(self.call)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        """Check if the batch invitation has expired."""
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    @property
    def effective_status(self) -> str:
        """The batch status with the deadline applied.

        A sent batch past its deadline is expired, whether or not the periodic
        expiry task has stored that yet.
        """
        if self.status == AssignmentBatchStatuses.SENT and self.is_expired:
            return AssignmentBatchStatuses.EXPIRED
        return self.status

    def get_effective_status_display(self) -> str:
        return str(dict(AssignmentBatchStatuses.CHOICES)[self.effective_status])

    @property
    def items_pending_count(self) -> int:
        """Count of items still awaiting a response that can still be given."""
        if self.is_expired:
            return 0
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
        # The caller queues the invitation email
        # (tasks.send_assignment_batch_invitation) once this returns.


def filter_assignment_items(user):
    """Filter assignment items based on user's roles."""
    return (
        # Reviewer's own items, once their batch has been sent
        Q(
            batch__reviewer_pool_entry__reviewer__user=user,
            batch__status__in=AssignmentBatchStatuses.SENT_TO_REVIEWER,
        )
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
        ordering = ["-affinity_score", "id"]
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Assignment: {self.proposal.name} -> {self.batch}"

    @classmethod
    def get_url_name(cls):
        return "assignment-item"

    @property
    def expires_at(self) -> datetime | None:
        """Deadline for responding to this item: its batch's deadline."""
        return self.batch.expires_at

    @property
    def is_expired(self) -> bool:
        """Whether this item can no longer be answered because time ran out."""
        if self.status == AssignmentItemStatuses.EXPIRED:
            return True
        return self.status == AssignmentItemStatuses.PENDING and self.batch.is_expired

    @property
    def effective_status(self) -> str:
        """The item status with the deadline applied.

        A pending item whose batch is past its deadline is expired, whether or
        not the periodic expiry task has stored that yet.
        """
        if self.is_expired:
            return AssignmentItemStatuses.EXPIRED
        return self.status

    def get_effective_status_display(self) -> str:
        return str(dict(AssignmentItemStatuses.CHOICES)[self.effective_status])

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

        if self.proposal.state not in ProposalStates.UNDER_EVALUATION:
            raise ValidationError(
                _("This proposal is no longer under evaluation and cannot be reviewed.")
            )

        # Create the Review record
        reviewer_profile = self.batch.reviewer_pool_entry.reviewer
        if not reviewer_profile:
            raise ValidationError(_("Reviewer profile not found."))
        # Taking on the review: refused to a reviewer the operator has
        # suspended or blocked since the offer, or outside the call's reviewer
        # eligibility, and, where disclosures are required, until one is filed.
        call = self.batch.call
        reviewer_standing.ensure_reviewer_eligible(reviewer_profile.user, call)
        reviewer_standing.ensure_coi_disclosed(reviewer_profile.user, call)

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


class StepContribution(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """One piece of work inside a proposal's workflow step.

    A slot is either per person (``contributor`` set from the start) or per
    requested offering (``requested_offering`` set; any member of that
    offering's team may fill it, and whoever submits it becomes its
    ``contributor``). A step's form answers are kept per contribution, in a
    checklist completion scoped to it, so two steps sharing a form, or two
    contributors to one step, never share answers. Scientific reviews keep
    their ``Review`` and ``AssignmentItem`` and are linked here.
    """

    proposal_step = models.ForeignKey(
        ProposalWorkflowStepInstance,
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    requested_offering = models.ForeignKey(
        RequestedOffering,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="step_contributions",
        help_text="The offering this slot assesses, for a per-offering slot.",
    )
    contributor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Who the contribution belongs to; for a per-offering slot, who "
            "submitted it."
        ),
    )
    is_lead = models.BooleanField(
        default=False,
        help_text="Whether the contributor leads the step's work.",
    )
    status = models.CharField(
        max_length=16,
        choices=enums.StepContributionStatuses.CHOICES,
        default=enums.StepContributionStatuses.PENDING,
        db_index=True,
    )
    outcome = models.CharField(
        max_length=32,
        blank=True,
        help_text="What the contribution concludes, e.g. conditionally_accepted.",
    )
    conditions = models.TextField(
        blank=True,
        help_text="Conditions of a conditional acceptance.",
    )
    comment = models.TextField(blank=True)
    review = models.OneToOneField(
        Review,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contribution",
    )
    assignment_item = models.OneToOneField(
        AssignmentItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contribution",
    )
    due_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the contribution is due; the step's deadline by default.",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    delegate = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Who the slot was handed to, as the step's delegable_to allows: "
            "while they stay eligible, they alone fill it once they accept "
            "(the slot is then accepted). The slot's holders keep reading it."
        ),
    )
    delegated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Who handed the slot to its delegate.",
    )
    delegated_at = models.DateTimeField(
        null=True, blank=True, help_text="When the slot was handed to its delegate."
    )
    coi_declaration_text = models.TextField(
        blank=True,
        help_text=(
            "The conflict-of-interest declaration the delegate confirmed on "
            "taking the slot over, as worded then; empty for the platform's "
            "standard one, or when the step asks for none."
        ),
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Step contribution")
        verbose_name_plural = _("Step contributions")
        constraints = [
            models.UniqueConstraint(
                fields=["proposal_step", "requested_offering"],
                condition=Q(requested_offering__isnull=False),
                name="proposal_unique_contribution_per_requested_offering",
            ),
            models.UniqueConstraint(
                fields=["proposal_step", "contributor"],
                condition=Q(
                    requested_offering__isnull=True,
                    review__isnull=True,
                    contributor__isnull=False,
                ),
                name="proposal_unique_person_contribution_per_step",
            ),
        ]

    def __str__(self):
        target = (
            self.requested_offering.offering.name
            if self.requested_offering_id
            else self.contributor.full_name
            if self.contributor_id
            else "-"
        )
        return f"{self.proposal_step} — {target} [{self.status}]"

    @property
    def is_per_offering(self) -> bool:
        return self.requested_offering_id is not None

    @property
    def proposal(self):
        return self.proposal_step.proposal

    def user_can_read_checklist(self, user) -> bool:
        """Whether ``user`` may open the answers, and files, of this form."""
        from waldur_mastermind.proposal import contributions

        return contributions.user_can_read(user, self)

    def get_checklist_completion(self, checklist):
        """This contribution's completion of ``checklist``, or None."""
        if checklist is None:
            return None
        # Loaded in bulk for a list of contributions; see prefetch_answers.
        prefetched = getattr(self, "_checklist_completions", None)
        if prefetched is not None:
            return prefetched.get(checklist.id)
        return checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=ContentType.objects.get_for_model(self),
            scope_object_id=self.id,
            checklist=checklist,
        ).first()

    def ensure_checklist_completion(self, checklist):
        """Get-or-create this contribution's completion of ``checklist``."""
        if checklist is None:
            return None
        completion, _ = checklist_models.ChecklistCompletion.objects.get_or_create(
            scope_content_type=ContentType.objects.get_for_model(self),
            scope_object_id=self.id,
            checklist=checklist,
        )
        return completion


class StepContributionCoiConfirmation(models.Model):
    """One confirmation of having no conflict of interest, given on taking a
    contribution over.

    Kept for good: handing the contribution on, taking it back or submitting
    it changes the contribution, never what someone confirmed. The wording
    confirmed is kept as it was then, with where it came from -- the step's,
    the call's, or the platform's standard declaration, which an empty text
    stands for.
    """

    class Sources:
        STEP = "step"
        CALL = "call"
        PLATFORM = "platform"

        CHOICES = (
            (STEP, "Step"),
            (CALL, "Call"),
            (PLATFORM, "Platform"),
        )

    contribution = models.ForeignKey(
        StepContribution, on_delete=models.CASCADE, related_name="coi_confirmations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        help_text="Who confirmed.",
    )
    confirmed_at = models.DateTimeField(default=timezone.now)
    declaration_text = models.TextField(
        blank=True,
        help_text=(
            "The declaration confirmed, as worded then; empty for the "
            "platform's standard one."
        ),
    )
    declaration_source = models.CharField(max_length=16, choices=Sources.CHOICES)

    class Meta:
        ordering = ["confirmed_at", "id"]
        verbose_name = _("Contribution conflict-of-interest confirmation")
        verbose_name_plural = _("Contribution conflict-of-interest confirmations")

    def __str__(self):
        return f"{self.contribution} — {self.user} at {self.confirmed_at}"


class ProposalChangeRequest(TimeStampedModel, core_models.UuidMixin):
    """One round of changes asked for on a proposal under evaluation.

    Proposal-level (``contribution`` empty): the actor of a step that
    ``allows_change_requests`` sends the proposal back to its applicant, who
    may then edit the ``unlocked_parts`` and submit the changes. The proposal
    stays ``in_review`` on its step meanwhile. Contribution-level: those
    overseeing the call reopen a submitted contribution for its contributor
    to submit again. Rows are kept once closed: they are the history of the
    rounds of changes.
    """

    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="change_requests"
    )
    step_instance = models.ForeignKey(
        ProposalWorkflowStepInstance,
        on_delete=models.CASCADE,
        related_name="change_requests",
    )
    contribution = models.ForeignKey(
        "StepContribution",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="change_requests",
        help_text="The contribution reopened; empty for a request to the applicant.",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    message = models.TextField(
        help_text=(
            "What is to change, for the applicant, or for the contributor of "
            "a reopened contribution."
        )
    )
    unlocked_parts = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "The parts of the proposal the applicant may edit while the "
            "request is open: field:<name>, documents, resources, team, "
            "compliance."
        ),
    )
    due_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the changes are due. Past it, the request lapses and the "
            "step returns to its actor."
        ),
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    resolution = models.CharField(
        max_length=16,
        choices=enums.ChangeRequestResolutions.CHOICES,
        blank=True,
        help_text="How the request was closed; empty while it is open.",
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Proposal change request")
        verbose_name_plural = _("Proposal change requests")
        constraints = [
            models.UniqueConstraint(
                fields=["step_instance"],
                condition=Q(closed_at__isnull=True, contribution__isnull=True),
                name="proposal_one_open_change_request_per_step",
            ),
            models.UniqueConstraint(
                fields=["contribution"],
                condition=Q(closed_at__isnull=True, contribution__isnull=False),
                name="proposal_one_open_change_request_per_contribution",
            ),
        ]

    def __str__(self):
        return (
            f"{self.proposal} — {self.step_instance.step} [{self.resolution or 'open'}]"
        )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class ProposalReviewerSuggestion(
    TimeStampedModel,
    core_models.UuidMixin,
):
    """A reviewer an evaluator suggests for one proposal.

    Made by a holder of an evaluator role (a rapporteur, say) through their
    contribution to a step that collects reviewer suggestions: an existing
    user or an e-mail address, with an optional note on their expertise.
    The call's managers turn it into an invitation to the call's reviewer
    pool, which it then follows (see ``current_status``). Evaluators' work:
    never shown to the applicant.
    """

    proposal = models.ForeignKey(
        Proposal,
        on_delete=models.CASCADE,
        related_name="reviewer_suggestions_by_evaluators",
    )
    contribution = models.ForeignKey(
        StepContribution,
        on_delete=models.CASCADE,
        related_name="reviewer_suggestions",
        help_text="The suggester's contribution to the step the suggestion came from.",
    )
    suggested_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    suggested_email = models.EmailField(
        blank=True,
        help_text="E-mail address of a suggested reviewer without a user account.",
    )
    note = models.TextField(blank=True, help_text="Why this reviewer: expertise.")
    pool_entry = models.ForeignKey(
        CallReviewerPool,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposal_reviewer_suggestions",
        help_text="The pool invitation the suggestion was turned into.",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    invited_at = models.DateTimeField(null=True, blank=True)
    discarded_at = models.DateTimeField(null=True, blank=True)
    discarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = _("Proposal reviewer suggestion")
        verbose_name_plural = _("Proposal reviewer suggestions")
        constraints = [
            models.CheckConstraint(
                condition=Q(suggested_user__isnull=False) | Q(suggested_email__gt=""),
                name="proposal_reviewer_suggestion_user_or_email",
            ),
            models.UniqueConstraint(
                fields=["contribution", "suggested_user"],
                condition=Q(suggested_user__isnull=False),
                name="proposal_unique_suggested_user_per_contribution",
            ),
            models.UniqueConstraint(
                fields=["contribution", "suggested_email"],
                condition=Q(suggested_user__isnull=True, suggested_email__gt=""),
                name="proposal_unique_suggested_email_per_contribution",
            ),
        ]

    def __str__(self):
        who = (
            self.suggested_user.full_name
            if self.suggested_user_id
            else self.suggested_email
        )
        return f"{who} for {self.proposal}"

    @property
    def suggester(self):
        return self.contribution.contributor

    @staticmethod
    def pool_entry_status(entry) -> str:
        """The suggestion status a pool invitation stands for."""
        Statuses = enums.ProposalReviewerSuggestionStatuses
        if entry.invitation_status == ReviewerPoolInvitationStatuses.ACCEPTED:
            return Statuses.ACCEPTED
        if entry.invitation_status == ReviewerPoolInvitationStatuses.DECLINED:
            return Statuses.DECLINED
        if entry.is_invitation_expired:
            return Statuses.EXPIRED
        return Statuses.INVITED

    @property
    def current_status(self) -> str:
        """Where the suggestion stands, following its pool invitation."""
        if self.pool_entry is not None:
            return self.pool_entry_status(self.pool_entry)
        if self.discarded_at is not None:
            return enums.ProposalReviewerSuggestionStatuses.DISCARDED
        return enums.ProposalReviewerSuggestionStatuses.PENDING


class ProposalRanking(TimeStampedModel, core_models.UuidMixin):
    """Where the committee placed a proposal in its round, and what it recommends.

    Kept per round rather than on the proposal: a ranking belongs to the
    cut-off it was made for, and a proposal moved to another round does not
    carry it there. Not a workflow step: it has no state, deadline or
    notification of its own. Fixed once the round goes to its decision body
    (``RoundLifecycleStates.RANKING_LOCKED``).
    """

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="rankings")
    proposal = models.ForeignKey(
        "Proposal", on_delete=models.CASCADE, related_name="rankings"
    )
    meeting_note = models.ForeignKey(
        "RoundMeetingNote",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rankings",
        help_text=(
            "The round report this ranking belongs to, such as a domain "
            "panel's ranking within its domain. Empty for the round's "
            "overall ranking."
        ),
    )
    # The report's id, 0 for the overall ranking: the uniqueness of ranks per
    # ranking is then a plain constraint Postgres can defer, which one over a
    # nullable column (NULLS NOT DISTINCT) is not.
    scope_key = models.PositiveIntegerField(default=0, editable=False)
    rank = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Position of the proposal in its round, 1 being the first.",
    )
    final_score = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="The committee's final score for the proposal.",
    )
    recommended_resources = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Recommended amounts per requested offering and component: "
            '{"<requested offering uuid>": {"<component type>": amount}}.'
        ),
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["round", "rank", "id"]
        constraints = [
            # One ranking per proposal in the round's overall ranking, and
            # one in each round report.
            models.UniqueConstraint(
                fields=["round", "scope_key", "proposal"],
                name="proposal_ranking_unique_per_round_and_proposal",
            ),
            # Deferred, so that an import can move ranks around within one
            # transaction; the serializers refuse a taken rank first.
            models.UniqueConstraint(
                fields=["round", "scope_key", "rank"],
                name="proposal_ranking_unique_rank_per_round",
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]

    def __str__(self):
        return f"{self.round} — {self.rank}. {self.proposal.name}"

    def save(self, *args, **kwargs):
        self.scope_key = self.meeting_note_id or 0
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "scope_key" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "scope_key"]
        super().save(*args, **kwargs)


class RoundMeetingNote(TimeStampedModel, core_models.UuidMixin):
    """Committee or panel feedback recorded for a round's cut-off as a whole.

    A note with a ``call_step`` is a round report: the work of one
    contributor of a round step (``CallWorkflowStep.round_scoped``), such as
    a domain panel chair's meeting report, covering the proposals of the
    round they hold that step for. It is written by its author alone and
    read by fewer people than a free note (``rankings``).
    """

    round = models.ForeignKey(
        Round, on_delete=models.CASCADE, related_name="meeting_notes"
    )
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    author = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    call_step = models.ForeignKey(
        CallWorkflowStep,
        # A report is the record of a step the round's proposals walked.
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="round_reports",
        help_text="The round step this note reports on; empty for a free note.",
    )
    proposals = models.ManyToManyField(
        "Proposal",
        blank=True,
        related_name="round_reports",
        help_text="The proposals a round report covers; fixed once submitted.",
    )
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created", "id"]
        constraints = [
            # One report per author of a round step in a round.
            models.UniqueConstraint(
                fields=["round", "call_step", "author"],
                condition=Q(call_step__isnull=False, author__isnull=False),
                name="proposal_round_report_unique_per_author",
            )
        ]

    def __str__(self):
        return self.title

    @property
    def is_report(self) -> bool:
        return self.call_step_id is not None

    def user_can_read_checklist(self, user) -> bool:
        """Whether ``user`` may open the answers, and files, of the report's
        form."""
        from waldur_mastermind.proposal import rankings

        return rankings.readable_meeting_notes(user).filter(pk=self.pk).exists()


class RoundMeetingNoteDocument(TimeStampedModel, core_models.UuidMixin):
    """A file attached to a round's meeting note: minutes, slides, a report."""

    note = models.ForeignKey(
        RoundMeetingNote, on_delete=models.CASCADE, related_name="documents"
    )
    file = models.FileField(upload_to="proposal_round_meeting_notes")

    class Meta:
        ordering = ["created", "id"]

    @property
    def name(self) -> str:
        return self.file.name.rsplit("/", 1)[-1] if self.file else ""


class RoundStep(TimeStampedModel, core_models.UuidMixin):
    """A call step in one round: its due date there, and for a round step
    (``CallWorkflowStep.round_scoped``) when the call's managers opened it.

    ``due_at`` is the deadline of the step for every proposal of the round,
    whatever its duration says. A round step waits for its opening: until
    then its proposals stand at it with nothing to do.
    """

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="steps")
    call_step = models.ForeignKey(
        CallWorkflowStep, on_delete=models.CASCADE, related_name="round_steps"
    )
    due_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the step is due in this round, for every proposal. Empty: "
            "the step's duration counts from when it starts."
        ),
    )
    opened_at = models.DateTimeField(null=True, blank=True, editable=False)
    opened_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    opened_reason = models.TextField(
        blank=True,
        editable=False,
        help_text="Why the step was opened while proposals had not reached it.",
    )
    sent_notifications = models.JSONField(
        default=list,
        blank=True,
        help_text="Round-level mail already sent for this step, by key.",
    )

    class Meta:
        ordering = ["round", "call_step__order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["round", "call_step"], name="proposal_unique_round_step"
            )
        ]

    def __str__(self):
        return f"{self.round} — {self.call_step.name or self.call_step.key}"

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None


class CallTemplate(TimeStampedModel, core_models.UuidMixin):
    """A call configuration a call manager can start a new call from.

    The document is in the format ``call_transfer`` exports and imports, and
    carries the call's settings and workflow only: never rounds, requested
    offerings, documents or people. Staff author templates, or load one from
    a preset shipped with the call module -- a copy that keeps no link to the
    preset; call managers see the published ones.
    """

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    document = models.JSONField()
    schema_version = models.PositiveSmallIntegerField()
    is_published = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    version = models.PositiveIntegerField(
        default=1,
        editable=False,
        help_text=(
            "Incremented whenever the document changes; name, description and "
            "publication leave it as it is."
        ),
    )
    scenarios = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Named paths through the workflow, each a dry run with what must "
            "hold at its end. They run on the calls created from the template."
        ),
    )

    calls: models.Manager["Call"]
    versions: models.Manager["WorkflowVersion"]

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "call-template"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.pk and (update_fields is None or "document" in update_fields):
            stored = (
                CallTemplate.objects.filter(pk=self.pk)
                .values_list("document", flat=True)
                .first()
            )
            if stored is not None and document_hash(stored) != document_hash(
                self.document
            ):
                self.version += 1
                if update_fields is not None:
                    kwargs["update_fields"] = [*update_fields, "version"]
        super().save(*args, **kwargs)


def document_hash(document) -> str:
    """A digest of a call document that ignores key order and formatting.

    The database stores documents as jsonb, which reorders keys, so the
    digest is taken over a canonical serialisation.
    """
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class WorkflowVersion(TimeStampedModel):
    """A snapshot of a call's or a call template's settings and workflow.

    The document is in the call-transfer format a template has: settings and
    workflow, never rounds, offerings, documents or people. Versions of a
    draft call by one user within a few minutes of each other are merged into
    the latest; from activation on, every version is kept.
    """

    class Reasons:
        CREATED = "created"
        CREATED_FROM_TEMPLATE = "created_from_template"
        IMPORTED = "imported"
        DUPLICATED = "duplicated"
        EDITED = "edited"
        ACTIVATED = "activated"

        CHOICES = (
            (CREATED, "Created"),
            (CREATED_FROM_TEMPLATE, "Created from a template"),
            (IMPORTED, "Imported"),
            (DUPLICATED, "Duplicated"),
            (EDITED, "Edited"),
            (ACTIVATED, "Activated"),
        )

    call = models.ForeignKey(
        Call,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workflow_versions",
    )
    template = models.ForeignKey(
        CallTemplate,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    number = models.PositiveIntegerField()
    document = models.JSONField()
    document_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        core_models.User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reason = models.CharField(max_length=32, choices=Reasons.CHOICES)
    summary = models.JSONField(
        default=dict,
        blank=True,
        help_text="Counts of the changes from the previous version.",
    )
    checks = models.JSONField(
        default=dict,
        blank=True,
        help_text="Results of checks run on this version.",
    )

    class Meta:
        ordering = ["number", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(call__isnull=False, template__isnull=True)
                    | Q(call__isnull=True, template__isnull=False)
                ),
                name="proposal_workflow_version_one_owner",
            ),
            models.UniqueConstraint(
                fields=["call", "number"],
                condition=Q(call__isnull=False),
                name="proposal_unique_call_workflow_version",
            ),
            models.UniqueConstraint(
                fields=["template", "number"],
                condition=Q(template__isnull=False),
                name="proposal_unique_template_workflow_version",
            ),
        ]

    def __str__(self):
        return f"{self.call or self.template} v{self.number}"
