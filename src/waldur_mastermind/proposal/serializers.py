import hashlib
import json
import logging
import re
import uuid
from datetime import datetime

import yaml
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.drainage import set_override
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.reverse import reverse

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.configuration import config
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core.enums import ReviewStates as CoreReviewStates
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.models import DESCRIPTION_LENGTH
from waldur_core.core.validators import get_project_name_regex_error
from waldur_core.media import models as media_models
from waldur_core.media.validators import DocumentValidator, ImageValidator
from waldur_core.permissions import enums as permissions_enums
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import Role
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.managers import filter_orders_for_user
from waldur_mastermind.marketplace.serializers import (
    BasePublicPlanSerializer,
    LimitBoundField,
    LimitValueField,
    OfferingComponentSerializer,
    OfferingOptionsField,
    UserAttributeConfigBaseSerializer,
    validate_prepaid_duration_against_component,
)
from waldur_mastermind.proposal import (
    activity,
    call_templates,
    change_requests,
    coi_declarations,
    my_tasks,
    notification_overrides,
    offering_demand,
    provider_calls,
    reviewer_stats,
    runs,
)
from waldur_mastermind.proposal import (
    permissions as proposal_permissions,
)
from waldur_mastermind.proposal.enums import (
    AWARD_RESPONSE_KEY,
    COMMON_STEP_FIELDS,
    DEFAULT_CALL_STEPS_MAP,
    PROJECT_NAME_PLACEHOLDERS,
    PROJECT_NAME_TEMPLATE_HELP,
    PROPOSAL_CONFIGURABLE_FIELDS,
    AllocationTimes,
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    BulkRoundCadence,
    CallStates,
    ChangeRequestParts,
    CheckRunStatuses,
    COISeverityLevels,
    COITypes,
    ContributionOutcomes,
    ContributorSources,
    DelegationTargets,
    EvaluationStart,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    OnNegativeOutcomes,
    OrderAuthors,
    OutcomeSets,
    PostAwardStatuses,
    ProposalFieldStates,
    ProposalRunStates,
    ProposalStates,
    RequestedOfferingStates,
    ResourceTemplateSelectionModes,
    ResponsibleRoles,
    ResultsPublication,
    ReviewerPoolInvitationStatuses,
    ReviewerStandings,
    RoundStatuses,
    StepContributionStatuses,
    StepKinds,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

from . import (
    allocation_names,
    budgets,
    contributions,
    enums,
    models,
    notification_rules,
    panels,
    proposal_roles,
    rankings,
    reviewer_suggestions,
    round_reports,
    step_inputs,
    utils,
    workflow_service,
)
from . import status_labels as step_status_labels
from .managers import (
    get_connected_calls,
    get_offering_manager_proposals,
    get_reviewed_proposals,
    round_open_q,
    round_overlaps,
)

logger = logging.getLogger(__name__)


# Maps applicant attribute name (from CallApplicantVisibilityConfig.expose_*)
# to ProposalSerializer field names that should be filtered when a reviewer
# views a proposal and the attribute is not exposed. ``approved_by`` is who
# accepted the award, the applicant on a call where they respond to it.
APPLICANT_FIELD_MAP: dict[str, list[str]] = {
    "full_name": [
        "created_by_name",
        "approved_by_name",
        "applicant_full_name",
        "applicant_first_name",
        "applicant_last_name",
    ],
    "username": [
        "created_by",
        "created_by_uuid",
        "approved_by",
        "applicant_username",
    ],
    "email": ["applicant_email"],
    "registration_method": ["applicant_registration_method"],
    "phone_number": ["applicant_phone_number"],
    "organization": ["applicant_organization"],
    "organization_country": ["applicant_organization_country"],
    "organization_type": ["applicant_organization_type"],
    "organization_registry_code": ["applicant_organization_registry_code"],
    "organization_vat_code": ["applicant_organization_vat_code"],
    "organization_address": ["applicant_organization_address"],
    "job_title": ["applicant_job_title"],
    "affiliations": ["applicant_affiliations"],
    "gender": ["applicant_gender"],
    "personal_title": ["applicant_personal_title"],
    "place_of_birth": ["applicant_place_of_birth"],
    "address": ["applicant_address"],
    "country_of_residence": ["applicant_country_of_residence"],
    "nationality": ["applicant_nationality"],
    "nationalities": ["applicant_nationalities"],
    "eduperson_assurance": ["applicant_eduperson_assurance"],
    "identity_source": ["applicant_identity_source"],
    "civil_number": ["applicant_civil_number"],
    "birth_date": ["applicant_birth_date"],
    "active_isds": ["applicant_active_isds"],
}


class ApplicantVisibility:
    """Whether one viewer sees proposals solely as an evaluator, and which
    applicant attributes the call's visibility config then conceals.

    Full-data viewers are staff, support, anyone holding a role on the
    proposal itself (the applicant team), and whoever
    manages the call -- a call manager, or an organiser through the managing
    organisation. Among the rest, evaluators are call reviewers, panel members,
    anyone holding a live review of the proposal (an accepted assignment
    grants no role), the holders of an evaluator role on it, such as its
    rapporteurs, and the providers: the managers of an offering the proposal
    requested or was awarded, who assess it and later serve the award. They
    see only what the call exposes. Anyone else is not an evaluator and sees
    whatever their access otherwise gives them.

    Lookups are per viewer, per call or per proposal set and are memoised, so
    one instance can resolve a whole listing without a query per row.
    """

    def __init__(self, user):
        self.user = user
        self._evaluator_call_ids = None
        self._reviewed_proposal_ids = None
        self._held_proposal_ids = None
        self._provided_proposal_ids = None
        self._proposal_role_ids = None
        self._call_managed = {}
        self._concealed_by_call = {}

    def _evaluates(self, proposal) -> bool:
        if self._evaluator_call_ids is None:
            self._evaluator_call_ids = set(
                get_connected_calls(
                    self.user, [CallRole.REVIEWER, CallRole.PANEL_MEMBER]
                )
            )
        if proposal.round.call_id in self._evaluator_call_ids:
            return True
        if self._reviewed_proposal_ids is None:
            self._reviewed_proposal_ids = set(get_reviewed_proposals(self.user))
        if proposal.id in self._reviewed_proposal_ids:
            return True
        if self._held_proposal_ids is None:
            self._held_proposal_ids = set(proposal_roles.proposals_held_by(self.user))
        if proposal.id in self._held_proposal_ids:
            return True
        if self._provided_proposal_ids is None:
            self._provided_proposal_ids = set(get_offering_manager_proposals(self.user))
        return proposal.id in self._provided_proposal_ids

    def _on_applicant_team(self, proposal) -> bool:
        # The current team, as ``proposal_roles.is_on_applicant_team`` reads
        # it: an author removed from the team is no longer the applicant.
        if self._proposal_role_ids is None:
            self._proposal_role_ids = set(
                proposal_roles.applicant_team_proposal_ids(self.user)
            )
        return proposal.id in self._proposal_role_ids

    def _manages_call(self, call) -> bool:
        if call.id not in self._call_managed:
            self._call_managed[call.id] = call.id in get_connected_calls(
                self.user, CallRole.MANAGER
            ) or permissions_utils.has_permission_on_any_source(
                self.user,
                permissions_enums.PermissionEnum.UPDATE_CALL,
                call,
                proposal_permissions.CALL_PERMISSION_SOURCES,
            )
        return self._call_managed[call.id]

    def is_reviewer_only(self, proposal) -> bool:
        user = self.user
        if not user or user.is_anonymous:
            return False
        if user.is_staff or user.is_support:
            return False
        if not self._evaluates(proposal):
            return False
        if self._on_applicant_team(proposal):
            return False
        return not self._manages_call(proposal.round.call)

    def concealed_attributes(self, proposal) -> set[str] | None:
        """None for a viewer who is not solely an evaluator of ``proposal``;
        otherwise the attributes its call's config conceals."""
        if not self.is_reviewer_only(proposal):
            return None
        call_id = proposal.round.call_id
        if call_id not in self._concealed_by_call:
            config_model = models.CallApplicantVisibilityConfig
            exposed = set(config_model.get_exposed_fields_for_call(proposal.round.call))
            self._concealed_by_call[call_id] = (
                set(config_model.get_attribute_names()) - exposed
            )
        return set(self._concealed_by_call[call_id])


class ReviewerVisibility:
    """Whether one viewer may know who wrote a review.

    Staff, support, the reviewer and the call team read every review in full.
    The applicant side -- the proposal's author and its team -- reads it as
    the applicant and learns the reviewer only where the call shows reviewers
    to submitters. Anyone else is kept from a blind review's author unless
    they oversee the proposal's call.

    Answers are per review and memoised per proposal and per call, so one
    instance can resolve a whole listing without a query per row.
    """

    def __init__(self, user):
        self.user = user
        self._applicant_side = {}
        self._managed_calls = {}
        self._overseen_proposals = {}

    def access(self, review) -> str:
        """``full``, ``applicant`` or ``other``: what of ``review`` the user
        reads. Decided per review, so a listing of several reviewers' reviews
        never takes one review's answer for all of them."""
        user = self.user
        if user.is_staff or user.is_support or review.reviewer_id == user.id:
            return "full"
        # The applicant side reads the review as the applicant, even when it
        # also manages the call.
        if review.proposal_id not in self._applicant_side:
            self._applicant_side[review.proposal_id] = (
                proposal_roles.is_on_applicant_team(user, review.proposal)
            )
        if self._applicant_side[review.proposal_id]:
            return "applicant"
        call = review.proposal.round.call
        if call.pk not in self._managed_calls:
            # The call team: its managers and the managing organisation's
            # organisers -- not every role there (an observer of the results).
            self._managed_calls[call.pk] = proposal_permissions.manages_call_workflow(
                user, call
            )
        return "full" if self._managed_calls[call.pk] else "other"

    def blind_to(self, review) -> bool:
        """Whether the user, reading another's review neither as the call
        team nor as the applicant -- a fellow evaluator, say, reading it as
        the input of a later step -- is kept from who wrote it: the review
        was blind. Those overseeing the call still see who did."""
        if not contributions.review_is_blind(review):
            return False
        # Per proposal: nobody oversees one they apply with.
        if review.proposal_id not in self._overseen_proposals:
            self._overseen_proposals[review.proposal_id] = (
                proposal_permissions.oversees_proposal_call(self.user, review.proposal)
            )
        return not self._overseen_proposals[review.proposal_id]

    def reveals_reviewer(self, review, access=None) -> bool:
        access = access or self.access(review)
        if access == "full":
            return True
        call = review.proposal.round.call
        if access == "applicant":
            return call.reviewer_identity_visible_to_submitters
        return not self.blind_to(review)


class PanelVisibility:
    """Whether one viewer may know which panel evaluates a proposal.

    Staff and support always do; the applicant team never does, not even of
    a call it otherwise oversees; whoever oversees the call does; anyone else
    only as ``user_can_view_proposal_panel`` allows (a member of that panel).
    Memoised per viewer and per call, for a whole listing.
    """

    def __init__(self, user):
        self.user = user
        self._team_proposal_ids = None
        self._oversees_call = {}

    def sees(self, proposal) -> bool:
        user = self.user
        if user is None or user.is_anonymous:
            return False
        if user.is_staff or user.is_support:
            return True
        if self._team_proposal_ids is None:
            self._team_proposal_ids = set(
                proposal_roles.applicant_team_proposal_ids(user)
            )
        if proposal.pk in self._team_proposal_ids:
            return False
        call_id = proposal.round.call_id
        if call_id not in self._oversees_call:
            self._oversees_call[call_id] = proposal_permissions.oversees_proposal_call(
                user, proposal
            )
        if self._oversees_call[call_id]:
            return True
        return proposal_permissions.user_can_view_proposal_panel(user, proposal)


def _is_reviewer_only_view(user, proposal) -> bool:
    """True if the user views this proposal solely as an evaluator; see
    ``ApplicantVisibility``."""
    return ApplicantVisibility(user).is_reviewer_only(proposal)


# Maps applicant attribute name to the proposal team list (list_users) row
# keys that carry it. The team list is serialised by UserRoleDetailsSerializer;
# each row describes a team member and, through created_by_*, the member who
# granted the role (usually the applicant). As in APPLICANT_FIELD_MAP the user
# UUID is the identity link and follows username, and the avatar image follows
# full_name because it identifies the person as much as the name does. Row keys
# absent from this map (uuid, created, role_*, source) describe the grant, not
# the person; expiration_time is concealed separately.
TEAM_MEMBER_FIELD_MAP: dict[str, list[str]] = {
    "full_name": ["user_full_name", "created_by_full_name", "user_image"],
    "username": ["user_username", "user_uuid", "created_by_uuid"],
    "email": ["user_email"],
}

# Maps applicant attribute name to the list_users query parameters that match
# on it (filters, search and ordering keys). A viewer from whom the attribute
# is concealed may not use them: the size or order of the result would reveal
# the value even with the row keys dropped.
TEAM_MEMBER_QUERY_MAP: dict[str, dict[str, list[str]]] = {
    "full_name": {
        "params": ["full_name", "native_name", "search_string"],
        "ordering": ["full_name", "native_name"],
    },
    "username": {
        "params": ["username", "user_slug", "user", "user_url", "search_string"],
        "ordering": ["username"],
    },
    "email": {
        "params": ["search_string"],
        "ordering": ["email"],
    },
}


def get_concealed_applicant_attributes(user, proposal) -> set[str] | None:
    """Applicant attributes the call's visibility config conceals from ``user``.

    Returns None when the user does not view the proposal solely as a
    reviewer; such viewers see every attribute. Both the proposal payload and
    the proposal team list derive what to drop from this one set.
    """
    return ApplicantVisibility(user).concealed_attributes(proposal)


def get_applicant_visibility(context: dict, user) -> ApplicantVisibility:
    """The viewer's ApplicantVisibility, shared through a serializer context
    so that every row of one response reuses its lookups."""
    visibility = context.get("_applicant_visibility")
    if visibility is None or visibility.user != user:
        visibility = ApplicantVisibility(user)
        context["_applicant_visibility"] = visibility
    return visibility


def filter_applicant_fields_for_reviewer(
    data: dict, proposal, user, visibility: ApplicantVisibility | None = None
) -> dict:
    """Mutate the serialized representation to drop applicant fields that
    are not exposed by the call's visibility config when the user is a
    reviewer-only viewer."""
    visibility = visibility or ApplicantVisibility(user)
    concealed = visibility.concealed_attributes(proposal)
    for attr in concealed or ():
        for field_name in APPLICANT_FIELD_MAP.get(attr, []):
            data.pop(field_name, None)
    return data


def filter_team_member_fields(rows, concealed: set[str]):
    """Drop the team list row keys carrying a concealed applicant attribute."""
    for attr in concealed:
        for field_name in TEAM_MEMBER_FIELD_MAP.get(attr, []):
            for row in rows:
                row.pop(field_name, None)
    return rows


# Maps applicant attribute name to the keys of a checklist answer
# (AnswerSerializer) that identify the answer's author, a member of the
# applicant team. ``user`` is the author's primary key: an identity link, so it
# follows username like the other user identifiers.
CHECKLIST_ANSWER_FIELD_MAP: dict[str, list[str]] = {
    "full_name": ["user_name"],
    "username": ["user"],
}


def filter_checklist_answer_fields(questions, concealed: set[str]):
    """Drop the answer author keys carrying a concealed applicant attribute
    from each question's existing answer."""
    for question in questions:
        answer = question.get("existing_answer")
        if not answer:
            continue
        for attr in concealed:
            for field_name in CHECKLIST_ANSWER_FIELD_MAP.get(attr, []):
                answer.pop(field_name, None)
    return questions


def get_concealed_team_member_query(query_params, concealed: set[str]) -> list[str]:
    """Return the list_users query parameters that match on a concealed
    attribute, as they appear in the request."""
    requested_ordering = {
        key.strip().lstrip("-")
        for value in query_params.getlist("o")
        for key in value.split(",")
    }
    refused = []
    for attr in sorted(concealed):
        query = TEAM_MEMBER_QUERY_MAP.get(attr)
        if not query:
            continue
        refused.extend(param for param in query["params"] if param in query_params)
        if requested_ordering & set(query["ordering"]):
            refused.append("o")
    return sorted(set(refused))


class EligibilityCheckSerializer(serializers.Serializer):
    """Serializer for eligibility check response."""

    is_eligible = serializers.BooleanField()
    restrictions = serializers.ListField(child=serializers.CharField())


class NestedCallActionHyperlinkedRelatedField(serializers.HyperlinkedRelatedField):
    """
    HyperlinkedRelatedField for nested call actions that require two lookup fields:
    - uuid: the call's UUID (parent)
    - obj_uuid: the nested object's UUID (child)
    """

    def get_url(self, obj, view_name, request, format):
        if hasattr(obj, "pk") and obj.pk in (None, ""):
            return None

        kwargs = {
            "uuid": obj.call.uuid.hex,
            "obj_uuid": obj.uuid.hex,
        }
        return self.reverse(view_name, kwargs=kwargs, request=request, format=format)

    def get_object(self, view_name, view_args, view_kwargs):
        lookup_value = view_kwargs.get("obj_uuid")
        lookup_kwargs = {self.lookup_field: lookup_value}
        return self.get_queryset().get(**lookup_kwargs)


class CallManagingOrganisationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.CallManagingOrganisation
        fields = (
            "url",
            "uuid",
            "created",
            "description",
            "customer",
            "customer_name",
            "customer_uuid",
            "customer_image",
            "customer_abbreviation",
            "customer_native_name",
            "customer_country",
            "image",
            "max_active_proposals_per_applicant",
        )
        related_paths = {"customer": ("uuid", "name", "native_name", "abbreviation")}
        protected_fields = ("customer",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "customer": {"lookup_field": "uuid"},
        }

    customer_image = serializers.ImageField(source="customer.image", read_only=True)
    customer_country = serializers.CharField(source="customer.country", read_only=True)

    def validate(self, attrs):
        if not self.instance:
            marketplace_permissions.can_register_service_provider(
                self.context["request"], attrs["customer"]
            )
        return attrs


class NestedRequestedOfferingSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    # The plugin type drives the frontend's per-type component filter, which a
    # cost estimate has to apply or it prices components the offering hides.
    offering_type = serializers.ReadOnlyField(source="offering.type")
    category_uuid = serializers.UUIDField(
        read_only=True, source="offering.category.uuid"
    )
    category_name = serializers.ReadOnlyField(source="offering.category.title")
    provider_name = serializers.ReadOnlyField(source="offering.customer.name")
    call_managing_organisation = serializers.ReadOnlyField(
        source="call.manager.customer.name"
    )
    options = OfferingOptionsField(read_only=True, source="offering.options")
    plan_details = BasePublicPlanSerializer(read_only=True, source="plan")
    components = OfferingComponentSerializer(
        source="offering.components", many=True, read_only=True
    )

    class Meta:
        model = models.RequestedOffering
        fields = [
            "uuid",
            "state",
            "offering",
            "offering_name",
            "offering_uuid",
            "offering_type",
            "provider_name",
            "category_uuid",
            "category_name",
            "call_managing_organisation",
            "attributes",
            "plan",
            "plan_details",
            "options",
            "components",
            "require_purchase_order",
            "created",
        ]
        extra_kwargs = {
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-public-offering-detail",
            },
            "plan": {
                "lookup_field": "uuid",
                "view_name": "marketplace-plan-detail",
            },
        }

    def get_url(self, requested_offering) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-offering-detail",
                kwargs={
                    "uuid": requested_offering.call.uuid.hex,
                    "obj_uuid": requested_offering.uuid.hex,
                },
            )
        )


class NestedRequestedResourceSerializer(serializers.HyperlinkedModelSerializer):
    resource_name = serializers.ReadOnlyField(source="resource.name")
    requested_offering = NestedRequestedOfferingSerializer(read_only=True)
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    url = serializers.SerializerMethodField()
    call_resource_template_name = serializers.ReadOnlyField(
        source="call_resource_template.name"
    )
    call_resource_template = serializers.SerializerMethodField()

    def get_url(self, requested_resource) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-proposal-resource-detail",
                kwargs={
                    "uuid": requested_resource.proposal.uuid.hex,
                    "obj_uuid": requested_resource.uuid.hex,
                },
            )
        )

    def get_call_resource_template(self, requested_resource) -> str:
        if requested_resource.call_resource_template:
            return self.context["request"].build_absolute_uri(
                reverse(
                    "proposal-call-resource_template-detail",
                    kwargs={
                        "uuid": requested_resource.call_resource_template.call.uuid.hex,
                        "obj_uuid": requested_resource.call_resource_template.uuid.hex,
                    },
                )
            )
        return None

    # Whether this row needs a purchase order, resolved from the call's setting
    # so the form does not have to re-derive it from plugin_options.
    purchase_order_required = serializers.ReadOnlyField()
    has_purchase_order = serializers.ReadOnlyField()
    # Written through the dedicated multipart action, as orders do.
    attachment = serializers.FileField(read_only=True)
    # Copied verbatim into Order.limits when the proposal is allocated, so this
    # has to accept exactly what the ordering path accepts rather than staying
    # an untyped JSONField. Precision is settled per component by
    # validate_limits on the resulting order.
    limits = serializers.DictField(child=LimitValueField(), required=False)

    class Meta:
        model = models.RequestedResource
        fields = [
            "uuid",
            "url",
            "requested_offering",
            "resource",
            "resource_name",
            "call_resource_template",
            "call_resource_template_name",
            "attributes",
            "limits",
            "purchase_order_reference",
            "attachment",
            "purchase_order_required",
            "has_purchase_order",
            "description",
            "created_by",
            "created_by_name",
        ]
        extra_kwargs = {
            "resource": {
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
            "created_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }


class UserRequestedResourceSerializer(serializers.HyperlinkedModelSerializer):
    """One row of "resources I requested through a proposal".

    Deliberately flat rather than reusing ``NestedRequestedResourceSerializer``:
    that one embeds the whole requested offering (plan details, components,
    options), which is far more than a list needs and costs a query per row.

    Proposal state and resource state are reported separately. They are two
    different lifecycles — the resource does not exist until the proposal is
    approved — so collapsing them into one column would require inventing a
    mapping that neither model owns.
    """

    offering_name = serializers.CharField(
        read_only=True, source="requested_offering.offering.name"
    )
    offering_uuid = serializers.UUIDField(
        read_only=True, source="requested_offering.offering.uuid"
    )
    call_name = serializers.CharField(read_only=True, source="proposal.round.call.name")
    call_uuid = serializers.UUIDField(read_only=True, source="proposal.round.call.uuid")
    proposal_name = serializers.CharField(read_only=True, source="proposal.name")
    proposal_uuid = serializers.UUIDField(read_only=True, source="proposal.uuid")
    proposal_state = serializers.CharField(read_only=True, source="proposal.state")
    # resource is null until the proposal is approved. Without allow_null DRF
    # raises SkipField on the dotted source and drops the key from the payload
    # entirely, so the SDK sees an absent field rather than an explicit null.
    resource_name = serializers.CharField(
        read_only=True, source="resource.name", allow_null=True
    )
    resource_uuid = serializers.UUIDField(
        read_only=True, source="resource.uuid", allow_null=True
    )
    resource_state = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_resource_state(self, requested_resource) -> str | None:
        """Null until the proposal is approved and the resource is provisioned."""
        if not requested_resource.resource:
            return None
        return requested_resource.resource.get_state_display()

    class Meta:
        model = models.RequestedResource
        fields = [
            "uuid",
            "created",
            "description",
            "attributes",
            "limits",
            "offering_name",
            "offering_uuid",
            "call_name",
            "call_uuid",
            "proposal",
            "proposal_name",
            "proposal_uuid",
            "proposal_state",
            "resource_name",
            "resource_uuid",
            "resource_state",
        ]
        extra_kwargs = {
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
        }


# Each criterion is rated on the review's own 1-5 scale.
REVIEW_SCORE_MIN = enums.REVIEW_SCORE_MIN
REVIEW_SCORE_MAX = enums.REVIEW_SCORE_MAX


class ReviewCriterionSerializer(serializers.Serializer):
    uuid = serializers.CharField(help_text="Key of the criterion in criteria_scores.")
    name = serializers.CharField()
    order = serializers.IntegerField()
    weight = serializers.IntegerField(
        help_text="Weight in the overall score, relative to the step's other criteria."
    )


_criterion_key = contributions.criterion_key


def _criteria_of(review, context) -> list:
    """The review's criteria, looked up once per review and request."""
    cache = context.setdefault("_review_criteria", {})
    if review.pk not in cache:
        cache[review.pk] = contributions.review_criteria(review)
    return cache[review.pk]


def _summary_from_criteria(review, scores: dict, context) -> int:
    """The criterion scores' weighted average, rounded half up."""
    return contributions.weighted_review_score(_criteria_of(review, context), scores)


def validate_criteria_scores(review, scores, context) -> dict:
    """``scores`` keyed by hex criterion UUID, each one a criterion of the
    step the review is given in and rated 1-5."""
    if not isinstance(scores, dict):
        raise serializers.ValidationError(
            _("Expected an object of scores keyed by criterion.")
        )
    keys = {_criterion_key(c) for c in _criteria_of(review, context)}
    normalized = {}
    errors = {}
    for key, value in scores.items():
        try:
            hex_key = uuid.UUID(str(key)).hex
        except ValueError:
            hex_key = None
        if hex_key not in keys:
            errors[key] = _("Not a criterion of this review's step.")
            continue
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not REVIEW_SCORE_MIN <= value <= REVIEW_SCORE_MAX
        ):
            errors[key] = _("A score is a whole number from %(min)s to %(max)s.") % {
                "min": REVIEW_SCORE_MIN,
                "max": REVIEW_SCORE_MAX,
            }
            continue
        normalized[hex_key] = value
    if errors:
        raise serializers.ValidationError(errors)
    return normalized


class ProposalReviewSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round_uuid = serializers.UUIDField(source="proposal.round.uuid", read_only=True)
    round_cutoff_time = serializers.ReadOnlyField(source="proposal.round.cutoff_time")
    round_start_time = serializers.ReadOnlyField(source="proposal.round.start_time")
    round_name = serializers.ReadOnlyField(source="proposal.round.name")
    round_slug = serializers.ReadOnlyField(source="proposal.round.slug")
    call_uuid = serializers.UUIDField(source="proposal.round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="proposal.round.call.name")
    call_slug = serializers.ReadOnlyField(source="proposal.round.call.slug")
    call_managing_organisation_uuid = serializers.ReadOnlyField(
        source="proposal.round.call.manager.uuid"
    )
    reviewer_full_name = serializers.ReadOnlyField(source="reviewer.full_name")
    # How to reach the reviewer: only for those overseeing the call.
    reviewer_email = serializers.ReadOnlyField(source="reviewer.email")
    reviewer_organization = serializers.ReadOnlyField(source="reviewer.organization")
    reviewer_uuid = serializers.UUIDField(read_only=True, source="reviewer.uuid")
    reviewer_image = serializers.ImageField(source="reviewer.image", read_only=True)
    anonymous_reviewer_name = serializers.SerializerMethodField()

    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(read_only=True, source="proposal.uuid")
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    coi_confirmation_required = serializers.SerializerMethodField()
    coi_declaration_text = serializers.SerializerMethodField(
        help_text=(
            "The conflict-of-interest declaration the reviewer confirms: as "
            "confirmed once they did, else as worded now. Empty for the "
            "platform's standard declaration."
        )
    )
    override_workload_limit = serializers.BooleanField(
        write_only=True,
        required=False,
        help_text="Create the review even if this takes the reviewer above "
        "their maximum number of open assignments in the call's reviewer "
        "pool. The override is logged.",
    )
    criteria = serializers.SerializerMethodField(
        help_text=(
            "The criteria of the step the review is given in, each rated "
            "1-5 in criteria_scores and weighted in the summary_score. Empty "
            "when the step has none, and the review is then rated by its "
            "summary_score alone."
        )
    )
    criteria_scores = serializers.DictField(
        child=serializers.IntegerField(
            min_value=REVIEW_SCORE_MIN,
            max_value=REVIEW_SCORE_MAX,
            allow_null=True,
        ),
        required=False,
        help_text=(
            "Score per criterion, 1-5, keyed by criterion UUID. Writing it "
            "replaces the scores, and summary_score becomes their weighted "
            "average, rounded half up."
        ),
    )

    class Meta:
        model = models.Review
        fields = (
            "url",
            "uuid",
            "proposal",
            "proposal_name",
            "proposal_uuid",
            "proposal_slug",
            "reviewer",
            "reviewer_full_name",
            "reviewer_email",
            "reviewer_organization",
            "reviewer_uuid",
            "reviewer_image",
            "anonymous_reviewer_name",
            "state",
            "review_end_date",
            "summary_score",
            "criteria",
            "criteria_scores",
            "summary_public_comment",
            "summary_private_comment",
            "round_uuid",
            "round_name",
            "round_slug",
            "round_cutoff_time",
            "round_start_time",
            "call_name",
            "call_uuid",
            "call_slug",
            "call_managing_organisation_uuid",
            "comment_project_title",
            "comment_project_summary",
            "comment_project_description",
            "comment_project_supporting_documentation",
            "comment_resource_requests",
            "comment_team",
            "coi_confirmed",
            "coi_confirmed_at",
            "coi_confirmation_required",
            "coi_declaration_text",
            "override_workload_limit",
            "submitted_at",
            "created",
            "modified",
        )
        read_only_fields = ("coi_confirmed", "coi_confirmed_at")
        protected_fields = ("proposal", "reviewer")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }

    def validate(self, attrs):
        if not self.instance:
            reviewer = attrs["reviewer"]
            proposal = attrs["proposal"]

            if reviewer not in proposal.round.call.reviewers:
                raise serializers.ValidationError(
                    {"reviewer": _("User is not reviewer.")}
                )
            # Scored once the review exists and its step is known.
            attrs.pop("criteria_scores", None)
        elif "criteria_scores" in attrs:
            scores = validate_criteria_scores(
                self.instance, attrs["criteria_scores"], self.context
            )
            attrs["criteria_scores"] = scores
            if scores:
                attrs["summary_score"] = _summary_from_criteria(
                    self.instance, scores, self.context
                )

        return attrs

    @extend_schema_field(ReviewCriterionSerializer(many=True))
    def get_criteria(self, obj):
        return [
            {
                "uuid": _criterion_key(c),
                "name": c.name,
                "order": c.order,
                "weight": c.weight,
            }
            for c in _criteria_of(obj, self.context)
        ]

    @extend_schema_field(serializers.BooleanField())
    def get_coi_confirmation_required(self, obj) -> bool:
        # True when the call has an enabled workflow step that requires the
        # reviewer to attest absence of conflict of interest. Drives the
        # confirmation checkbox in the review-submission UI.
        return models.CallWorkflowStep.objects.filter(
            call=obj.proposal.round.call_id,
            is_enabled=True,
            requires_coi_confirmation=True,
        ).exists()

    @extend_schema_field(serializers.CharField())
    def get_coi_declaration_text(self, obj):
        if obj.coi_confirmed:
            return obj.coi_declaration_text
        if not self.get_coi_confirmation_required(obj):
            return ""
        return coi_declarations.for_review(obj)

    def get_anonymous_reviewer_name(self, obj) -> str | None:
        """
        Generate an anonymous reviewer identifier like 'Reviewer 1', 'Reviewer 2'.
        Returns None if the review is not associated with a proposal.
        """
        if not obj.proposal:
            return None

        # The proposal's reviews in a stable order that says nothing about
        # who wrote them, numbered once per proposal for a whole listing.
        numbers = self.context.setdefault("_reviewer_numbers", {})
        if obj.proposal_id not in numbers:
            numbers[obj.proposal_id] = {
                pk: index
                for index, pk in enumerate(
                    obj.proposal.review_set.order_by(
                        *contributions.ANONYMOUS_ORDER
                    ).values_list("pk", flat=True),
                    start=1,
                )
            }
        index = numbers[obj.proposal_id].get(obj.pk)
        if index is None:
            return _("Reviewer")
        return _("Reviewer %(index)s") % {"index": index}

    def _reviewer_visibility(self, user) -> "ReviewerVisibility":
        # One per listing, so its memos span every row.
        cached = self.context.get("_reviewer_visibility")
        if cached is None or cached.user != user:
            cached = self.context["_reviewer_visibility"] = ReviewerVisibility(user)
        return cached

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not isinstance(instance, models.Review):
            return data
        visibility = self._reviewer_visibility(user)
        access = visibility.access(instance)
        if access == "full":
            data.pop("anonymous_reviewer_name", None)
            return data
        # How to reach the reviewer is for those overseeing the call alone,
        # even where the call shows the applicant who reviewed.
        data.pop("reviewer_email", None)
        data.pop("reviewer_organization", None)
        if not visibility.reveals_reviewer(instance, access):
            # Hide real reviewer info, show anonymous identifier
            for field in (
                "reviewer",
                "reviewer_full_name",
                "reviewer_uuid",
                "reviewer_image",
            ):
                data.pop(field, None)
        else:
            # Show real reviewer info, hide anonymous identifier
            data.pop("anonymous_reviewer_name", None)
        # Always remove private comments for non-authorized users
        data.pop("summary_private_comment", None)
        return data

    def create(self, validated_data):
        """
        Prevent creating a duplicate review for the same proposal and reviewer, excluding rejected reviews.
        """
        reviewer = validated_data["reviewer"]
        proposal = validated_data["proposal"]

        existing_review = models.Review.objects.filter(
            proposal=proposal,
            reviewer=reviewer,
            state__in=[
                models.Review.States.SUBMITTED,
                models.Review.States.IN_REVIEW,
            ],
        ).exists()

        if existing_review:
            raise serializers.ValidationError(
                _("Review already exists for this proposal and reviewer.")
            )

        validated_data.pop("override_workload_limit", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("override_workload_limit", None)
        return super().update(instance, validated_data)


set_override(
    ProposalReviewSerializer,
    "optional_fields",
    [
        "anonymous_reviewer_name",
        "reviewer",
        "reviewer_full_name",
        "reviewer_uuid",
        "summary_private_comment",
    ],
)


class ReviewSubmitSerializer(serializers.ModelSerializer):
    criteria_scores = serializers.DictField(
        child=serializers.IntegerField(
            min_value=REVIEW_SCORE_MIN,
            max_value=REVIEW_SCORE_MAX,
            allow_null=True,
        ),
        required=False,
        help_text=(
            "Score per criterion, keyed by criterion UUID; the review's saved "
            "scores when left out. Every criterion of the step must be scored."
        ),
    )

    class Meta:
        model = models.Review
        fields = (
            "summary_score",
            "criteria_scores",
            "summary_public_comment",
            "summary_private_comment",
            "coi_confirmed",
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        review = self.instance
        criteria = _criteria_of(review, self.context)
        if criteria:
            scores = validate_criteria_scores(
                review,
                attrs.get("criteria_scores", review.criteria_scores),
                self.context,
            )
            unscored = [c.name for c in criteria if _criterion_key(c) not in scores]
            if unscored:
                raise serializers.ValidationError(
                    {
                        "criteria_scores": _("Score every criterion: %(names)s.")
                        % {"names": ", ".join(unscored)}
                    }
                )
            attrs["criteria_scores"] = scores
            attrs["summary_score"] = _summary_from_criteria(
                review, scores, self.context
            )
        else:
            attrs.pop("criteria_scores", None)
        # A conflict-of-interest attestation is required only when the call has
        # an enabled workflow step configured with requires_coi_confirmation.
        # The flag is per-step, but the attestation is per-review, so any such
        # enabled step on the call triggers the requirement.
        requires_coi = models.CallWorkflowStep.objects.filter(
            call=review.proposal.round.call_id,
            is_enabled=True,
            requires_coi_confirmation=True,
        ).exists()
        coi_confirmed = attrs.get("coi_confirmed", review.coi_confirmed)
        if requires_coi and not coi_confirmed:
            raise serializers.ValidationError(
                {
                    "coi_confirmed": _(
                        "You must confirm absence of conflict of interest "
                        "before submitting this review."
                    )
                }
            )
        return attrs

    def update(self, instance, validated_data):
        if "coi_confirmed" in validated_data:
            if validated_data["coi_confirmed"]:
                if not instance.coi_confirmed_at:
                    validated_data["coi_confirmed_at"] = timezone.now()
                    # The declaration confirmed, as worded now.
                    validated_data["coi_declaration_text"] = (
                        coi_declarations.for_review(instance)
                    )
            else:
                # Keep the timestamp consistent with the flag: an unconfirmed
                # review must not carry a stale confirmation time.
                validated_data["coi_confirmed_at"] = None
                validated_data["coi_declaration_text"] = ""
        return super().update(instance, validated_data)


class ProtectedProposalListSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField()
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    approved_by_name = serializers.ReadOnlyField(source="approved_by.full_name")
    reviews = serializers.SerializerMethodField()

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
            "slug",
            "name",
            "state",
            "reviews",
            "approved_by_name",
            "created_by_name",
            "created",
        ]
        extra_kwargs = {
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "approved_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_reviews(self, obj) -> list:
        """
        Return serialized reviews based on user permissions and visibility settings.
        - Staff, call managers, and reviewers see all reviews.
        - Submitters see submitted reviews if visibility is enabled.
        """
        request = self.context.get("request")
        user = getattr(request, "user", None)

        if not user or not request:
            return []

        reviews_qs = obj.review_set.all()
        on_team = proposal_roles.is_on_applicant_team(user, obj)

        if (
            user.is_staff
            or (
                not on_team
                and proposal_permissions.manages_call_workflow(user, obj.round.call)
            )
            or (not on_team and reviews_qs.filter(reviewer=user).exists())
        ):
            return ProposalReviewSerializer(
                reviews_qs, many=True, context=self.context
            ).data

        # The applicant team's view
        if (
            obj.round.call.reviews_visible_to_submitters
            and (
                obj.state == models.Proposal.States.ACCEPTED
                or obj.state == models.Proposal.States.REJECTED
            )
            and on_team
        ):
            submitted_reviews = reviews_qs.filter(state=models.Review.States.SUBMITTED)
            return ProposalReviewSerializer(
                submitted_reviews, many=True, context=self.context
            ).data

        return []


class NestedRoundSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Round
        fields = [
            "uuid",
            "slug",
            "name",
            "start_time",
            "cutoff_time",
            "status",
            "allocation_date",
            "review_duration_in_days",
            "lifecycle_state",
            "evaluation_started_at",
            "deciding_started_at",
            "results_published_at",
            "closed_at",
            "adopted_at",
        ]
        read_only_fields = [
            "lifecycle_state",
            "evaluation_started_at",
            "deciding_started_at",
            "results_published_at",
            "closed_at",
            "adopted_at",
        ]
        extra_kwargs = {
            "slug": {"required": False},
        }


class FileSizeField(serializers.IntegerField):
    """Size in bytes of the instance's ``file``, or null when there is none.

    ``FieldFile.size`` raises ``ValueError`` when no file is associated and
    ``OSError`` when the stored file is gone; either would fail the whole
    response that serialises the document.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("source", "file")
        kwargs["read_only"] = True
        kwargs["allow_null"] = True
        super().__init__(**kwargs)

    def to_representation(self, value):
        if not value:
            return None
        try:
            return value.size
        except (ValueError, OSError):
            return None


class CallDocumentSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = FileSizeField()

    class Meta:
        model = models.CallDocument
        fields = ["uuid", "file", "file_name", "file_size", "description", "created"]


class CallNotArchivedCreateMixin:
    """Provide the ``validate_call_not_archived`` hook used by
    ``ActionMethodMixin.action_list_method``'s ``additional_validators``.

    The hook is looked up by name on the serializer and called with the parent
    Call. It keeps archived calls read-only across their nested-create surface
    (offerings / resource templates / workflow steps).
    """

    def validate_call_not_archived(self, call):
        if call.state == CallStates.ARCHIVED:
            raise serializers.ValidationError(_("Cannot modify an archived call."))


class CallResourceTemplateSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    requested_offering_name = serializers.ReadOnlyField(
        source="requested_offering.offering.name"
    )
    requested_offering_plan = BasePublicPlanSerializer(
        read_only=True, source="requested_offering.plan"
    )
    requested_offering_uuid = serializers.UUIDField(
        source="requested_offering.uuid", read_only=True
    )
    # The plan alone cannot be priced: bucketing an amount into recurring or
    # one-off needs each component's billing type and limit period, and the
    # plugin type drives the frontend's component filter. Same two fields
    # NestedRequestedOfferingSerializer carries for the non-template path.
    requested_offering_type = serializers.ReadOnlyField(
        source="requested_offering.offering.type"
    )
    requested_offering_components = OfferingComponentSerializer(
        source="requested_offering.offering.components", many=True, read_only=True
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    url = serializers.SerializerMethodField()
    requested_offering = NestedCallActionHyperlinkedRelatedField(
        queryset=models.RequestedOffering.objects.all(),
        view_name="proposal-call-offering-detail",
        lookup_field="uuid",
    )
    # Copied verbatim into Order.limits when the proposal is allocated, so it
    # has to accept exactly what the ordering path accepts. Precision is
    # settled per component by validate_limits on that order.
    limits = serializers.DictField(child=LimitValueField(), required=False)

    class Meta:
        model = models.CallResourceTemplate
        fields = [
            "uuid",
            "url",
            "name",
            "description",
            "attributes",
            "limits",
            "is_required",
            "requested_offering",
            "requested_offering_name",
            "requested_offering_uuid",
            "requested_offering_plan",
            "requested_offering_type",
            "requested_offering_components",
            "created_by",
            "created_by_name",
            "created",
        ]
        read_only_fields = ("created_by",)
        extra_kwargs = {
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_fields(self):
        """Make requested_offering not required for PATCH operations."""
        fields = super().get_fields()
        if hasattr(self, "instance") and self.instance:
            if "requested_offering" in fields:
                fields["requested_offering"].required = False

        return fields

    def get_url(self, resource_template) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-resource_template-detail",
                kwargs={
                    "uuid": resource_template.call.uuid.hex,
                    "obj_uuid": resource_template.uuid.hex,
                },
            )
        )

    def validate_requested_offering(self, requested_offering):
        if hasattr(self, "initial_data") and "call" in self.context:
            call = self.context["call"]
            if requested_offering.call != call:
                raise serializers.ValidationError(
                    "Requested offering must belong to the same call"
                )
            if requested_offering.state != RequestedOfferingStates.ACCEPTED:
                raise serializers.ValidationError(
                    "Requested offering must be in accepted state"
                )
        return requested_offering

    def validate(self, attrs):
        # Requests take these values unchanged, so they are checked here,
        # where the call manager can still correct them.
        if "attributes" in attrs or "requested_offering" in attrs:
            requested_offering = attrs.get("requested_offering") or getattr(
                self.instance, "requested_offering", None
            )
            attributes = attrs.get(
                "attributes", getattr(self.instance, "attributes", None)
            )
            if requested_offering:
                offering = requested_offering.offering
                utils.normalize_option_values(attributes, offering)
                _validate_prepaid_duration(attributes, offering, None)
                # A request made from the template cannot add what it leaves
                # out, so a required option missing here would block every
                # proposal using it.
                _validate_required_options(attributes, offering)
        # A template is a default the applicant starts from: it must lie
        # inside the bounds the call's budgets set for its offering.
        requested_offering = attrs.get("requested_offering") or (
            self.instance.requested_offering if self.instance else None
        )
        limits = attrs.get("limits", self.instance.limits if self.instance else {})
        if requested_offering is not None:
            conflicts = budgets.template_conflicts(requested_offering, limits)
            if conflicts:
                raise serializers.ValidationError({"limits": conflicts})
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        template = super().update(instance, validated_data)
        # Drafts follow the template they were made from; submitted proposals
        # keep what they were submitted with.
        limits, attributes = utils.template_request_values(template)
        models.RequestedResource.objects.filter(
            call_resource_template=template, proposal__state=ProposalStates.DRAFT
        ).update(
            limits=limits,
            attributes=attributes,
            requested_offering=template.requested_offering,
        )
        return template


class RequestableComponentSerializer(serializers.Serializer):
    """A component of a call offering a proposal requests an amount of, as
    billed under the call's plan."""

    type = serializers.CharField(source="component.type")
    name = serializers.CharField(source="component.name")
    measured_unit = serializers.CharField(allow_blank=True)
    limit_period = serializers.CharField()
    unit_factor = serializers.IntegerField(source="component.unit_factor")
    min_value = LimitBoundField(source="component.min_value", allow_null=True)
    max_value = LimitBoundField(source="component.max_value", allow_null=True)
    limit_decimal_places = serializers.IntegerField(
        source="component.limit_decimal_places"
    )


def _rule_component(rule, context):
    """The component ``rule`` is of, as billed under the call's plan; looked
    up once per call offering for the whole response."""
    cache = context.setdefault("requestable_components", {})
    if rule.requested_offering_id not in cache:
        cache[rule.requested_offering_id] = (
            rule.requested_offering.requestable_components()
        )
    return cache[rule.requested_offering_id].get(rule.component_type)


class CallOfferingComponentBoundsSerializer(serializers.ModelSerializer):
    """What an applicant's request of a call offering's component is held
    to, in the component's unit and per its limit period."""

    requested_offering_uuid = serializers.UUIDField(
        source="requested_offering.uuid", read_only=True
    )
    min_per_proposal = LimitBoundField(read_only=True, allow_null=True)
    max_per_proposal = LimitBoundField(read_only=True, allow_null=True)
    default_amount = LimitBoundField(read_only=True, allow_null=True)
    measured_unit = serializers.SerializerMethodField()
    limit_period = serializers.SerializerMethodField()

    class Meta:
        model = models.CallOfferingComponent
        fields = [
            "uuid",
            "requested_offering_uuid",
            "component_type",
            "measured_unit",
            "limit_period",
            "min_per_proposal",
            "max_per_proposal",
            "default_amount",
            "is_fixed",
        ]
        read_only_fields = fields

    def get_measured_unit(self, rule) -> str:
        effective = _rule_component(rule, self.context)
        return effective.measured_unit if effective else ""

    def get_limit_period(self, rule) -> str:
        effective = _rule_component(rule, self.context)
        return effective.limit_period if effective else ""


class CallOfferingComponentSerializer(
    CallNotArchivedCreateMixin, CallOfferingComponentBoundsSerializer
):
    """What a proposal may request of one component of a call offering: the
    least and most, or a fixed amount, and the amount the form starts from.

    Editable until a proposal requesting the offering is submitted; the
    resource pools of the call hang off these rows."""

    url = serializers.SerializerMethodField()
    requested_offering_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        source="requested_offering",
        queryset=models.RequestedOffering.objects.all(),
        help_text="An offering of the call.",
    )
    offering_name = serializers.ReadOnlyField(source="requested_offering.offering.name")
    component_name = serializers.SerializerMethodField()
    min_per_proposal = LimitBoundField(required=False, allow_null=True, min_value=0)
    max_per_proposal = LimitBoundField(required=False, allow_null=True, min_value=0)
    default_amount = LimitBoundField(required=False, allow_null=True, min_value=0)
    is_locked = serializers.SerializerMethodField(
        help_text=(
            "Whether a submitted proposal requests the offering: the rule "
            "can no longer change."
        )
    )

    class Meta:
        model = models.CallOfferingComponent
        fields = [
            "uuid",
            "url",
            "requested_offering_uuid",
            "offering_name",
            "component_type",
            "component_name",
            "measured_unit",
            "limit_period",
            "min_per_proposal",
            "max_per_proposal",
            "default_amount",
            "is_fixed",
            "is_locked",
            "created",
        ]
        read_only_fields = ("uuid", "created")
        # One rule per component, checked in validate().
        validators = []

    def get_fields(self):
        fields = super().get_fields()
        # The nested detail route saves PATCH as a full update: only what is
        # sent changes. The offering and component a rule is of are fixed.
        if self.instance is not None:
            for name in ("requested_offering_uuid", "component_type"):
                if name in fields:
                    fields[name].read_only = True
        return fields

    def get_url(self, rule) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-offering_component-detail",
                kwargs={
                    "uuid": rule.requested_offering.call.uuid.hex,
                    "obj_uuid": rule.uuid.hex,
                },
            )
        )

    def get_component_name(self, rule) -> str:
        effective = _rule_component(rule, self.context)
        return effective.component.name if effective else ""

    def get_is_locked(self, rule) -> bool:
        return budgets.rules_locked(rule.requested_offering)

    def validate(self, attrs):
        call = self.context["call"]
        requested_offering = (
            self.instance.requested_offering
            if self.instance
            else attrs["requested_offering"]
        )
        if requested_offering.call_id != call.pk:
            raise serializers.ValidationError(
                {"requested_offering_uuid": _("The offering is not part of this call.")}
            )
        if requested_offering.state == RequestedOfferingStates.CANCELED:
            raise serializers.ValidationError(
                {"requested_offering_uuid": _("The offering request was canceled.")}
            )
        if budgets.rules_locked(requested_offering):
            raise serializers.ValidationError(
                _(
                    "A submitted proposal requests this offering: its component "
                    "rules can no longer change."
                )
            )

        rule = models.CallOfferingComponent(
            pk=self.instance.pk if self.instance else None,
            requested_offering=requested_offering,
        )
        if self.instance:
            for field in self.instance._meta.concrete_fields:
                setattr(rule, field.attname, getattr(self.instance, field.attname))
        for key, value in attrs.items():
            setattr(rule, key, value)
        if rule.is_fixed and rule.default_amount is not None:
            if rule.min_per_proposal is None or "min_per_proposal" not in attrs:
                rule.min_per_proposal = rule.default_amount
            if rule.max_per_proposal is None or "max_per_proposal" not in attrs:
                rule.max_per_proposal = rule.default_amount
        try:
            rule.clean_fields(exclude=["uuid", "created", "modified"])
            rule.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

        effective = requested_offering.requestable_components().get(rule.component_type)
        for name in ("min_per_proposal", "max_per_proposal", "default_amount"):
            value = getattr(rule, name)
            if value is None or effective is None:
                continue
            try:
                marketplace_utils.validate_limit_precision(value, effective.component)
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({name: exc.detail})

        duplicate = models.CallOfferingComponent.objects.filter(
            requested_offering=requested_offering, component_type=rule.component_type
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError(
                {"component_type": _("The offering already has a rule for it.")}
            )
        conflicts = budgets.rule_template_conflicts(rule)
        if conflicts:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        _(
                            "A resource template of the offering is outside these bounds."
                        )
                    ]
                    + conflicts
                }
            )
        attrs["min_per_proposal"] = rule.min_per_proposal
        attrs["max_per_proposal"] = rule.max_per_proposal
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class CallResourcePoolSerializer(
    CallNotArchivedCreateMixin, serializers.ModelSerializer
):
    """How much of a call offering's component the call awards, over the
    whole call (no round) or one of its rounds."""

    url = serializers.SerializerMethodField()
    offering_component_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        source="offering_component",
        queryset=models.CallOfferingComponent.objects.all(),
        required=False,
        help_text=(
            "The component rule of a call offering the pool is of. Or name the "
            "offering and component instead: a component without a rule gets "
            "one bounding nothing."
        ),
    )
    requested_offering = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.RequestedOffering.objects.all(),
        write_only=True,
        required=False,
        help_text="With component: the call offering the pool is of.",
    )
    component = serializers.CharField(
        write_only=True,
        required=False,
        help_text="With requested_offering: the type of the component.",
    )
    requested_offering_uuid = serializers.UUIDField(
        source="offering_component.requested_offering.uuid", read_only=True
    )
    offering_name = serializers.ReadOnlyField(
        source="offering_component.requested_offering.offering.name"
    )
    component_type = serializers.ReadOnlyField(
        source="offering_component.component_type"
    )
    round_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        source="round",
        queryset=models.Round.objects.all(),
        required=False,
        allow_null=True,
        help_text="The round the pool covers. Empty: the whole call.",
    )
    round_name = serializers.ReadOnlyField(source="round.name", allow_null=True)
    offered_total = LimitBoundField(min_value=0)

    class Meta:
        model = models.CallResourcePool
        fields = [
            "uuid",
            "url",
            "offering_component_uuid",
            "requested_offering",
            "component",
            "requested_offering_uuid",
            "offering_name",
            "component_type",
            "round_uuid",
            "round_name",
            "offered_total",
            "created",
        ]
        read_only_fields = ("uuid", "created")
        # One pool per component and round, checked in validate() where the
        # round may be left out for the whole call.
        validators = []

    def get_fields(self):
        fields = super().get_fields()
        # Only the total of a pool changes: its component and round are what
        # it is.
        if self.instance is not None:
            for name in ("offering_component_uuid", "round_uuid"):
                if name in fields:
                    fields[name].read_only = True
        return fields

    def get_url(self, pool) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-resource_pool-detail",
                kwargs={
                    "uuid": pool.offering_component.requested_offering.call.uuid.hex,
                    "obj_uuid": pool.uuid.hex,
                },
            )
        )

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        call = self.context["call"]
        rule = attrs.get("offering_component") or self._rule_for(call, attrs)
        attrs.pop("requested_offering", None)
        attrs.pop("component", None)
        attrs["offering_component"] = rule
        if rule.requested_offering.call_id != call.pk:
            raise serializers.ValidationError(
                {"offering_component_uuid": _("The component is not of this call.")}
            )
        call_round = attrs.get("round")
        if call_round is not None and call_round.call_id != call.pk:
            raise serializers.ValidationError(
                {"round_uuid": _("The round must belong to this call.")}
            )
        if (
            rule.pk
            and models.CallResourcePool.objects.filter(
                offering_component=rule, round=call_round
            ).exists()
        ):
            raise serializers.ValidationError(
                {
                    "round_uuid": _(
                        "The component already has a pool for the whole call."
                    )
                    if call_round is None
                    else _("The component already has a pool for this round.")
                }
            )
        return attrs

    @staticmethod
    def _rule_for(call, attrs):
        """The rule of the component the request names by its offering and
        type; an unsaved one bounding nothing when there is none yet."""
        requested_offering = attrs.get("requested_offering")
        component_type = attrs.get("component")
        if requested_offering is None or not component_type:
            raise serializers.ValidationError(
                {
                    "offering_component_uuid": _(
                        "Name the component rule, or the offering and component."
                    )
                }
            )
        if (
            requested_offering.call_id != call.pk
            or requested_offering.state == RequestedOfferingStates.CANCELED
        ):
            raise serializers.ValidationError(
                {"requested_offering": _("The offering is not part of this call.")}
            )
        rule = models.CallOfferingComponent.objects.filter(
            requested_offering=requested_offering, component_type=component_type
        ).first()
        if rule is not None:
            return rule
        if component_type not in requested_offering.requestable_components():
            raise serializers.ValidationError(
                {
                    "component": _(
                        "The offering's plan does not price a component of this "
                        "type billed by its limit."
                    )
                }
            )
        return models.CallOfferingComponent(
            requested_offering=requested_offering, component_type=component_type
        )

    def create(self, validated_data):
        user = self.context["request"].user
        rule = validated_data["offering_component"]
        if rule.pk is None:
            # A rule bounding nothing changes no applicant's request, so it
            # is added even once the offering's rules are locked.
            rule.created_by = user
            rule.save()
        validated_data["created_by"] = user
        return super().create(validated_data)


class ResourcePoolCellSerializer(serializers.Serializer):
    round_uuid = serializers.UUIDField(
        source="round.uuid", allow_null=True, help_text="Empty: the whole call."
    )
    round_name = serializers.CharField(source="round.name", allow_null=True)
    pool_uuid = serializers.UUIDField(source="pool.uuid", allow_null=True)
    offered_total = LimitValueField(
        allow_null=True, help_text="Empty: the cell has no pool."
    )
    requested = LimitValueField()
    awarded = LimitValueField()
    remaining = LimitValueField(allow_null=True)


class ResourcePoolUsageSerializer(serializers.Serializer):
    """A component a proposal requests of the call's offerings, with its
    pools: a cell for the whole call, then one per round. Requested and awarded are counted as against
    a pool, over the whole length of each item."""

    offering_component_uuid = serializers.UUIDField(
        source="rule.uuid",
        allow_null=True,
        help_text=(
            "The component rule, empty while none is set: a proposal may then "
            "request any amount of it."
        ),
    )
    requested_offering_uuid = serializers.UUIDField(source="requested_offering.uuid")
    offering_name = serializers.CharField()
    component_type = serializers.CharField()
    component_name = serializers.CharField(allow_blank=True)
    measured_unit = serializers.CharField(allow_blank=True)
    limit_period = serializers.CharField(allow_blank=True)
    cells = ResourcePoolCellSerializer(many=True)


class ProposalBudgetEntrySerializer(serializers.Serializer):
    pool_uuid = serializers.UUIDField(source="pool.uuid")
    offering_name = serializers.CharField()
    component_type = serializers.CharField()
    round_name = serializers.CharField(
        source="round.name",
        allow_null=True,
        help_text="The round the pool covers; empty for the whole call.",
    )
    offered_total = LimitValueField()
    awarded_so_far = LimitValueField(
        help_text="Awarded to the other proposals the pool covers."
    )
    remaining = LimitValueField()
    award = LimitValueField(
        allow_null=True, help_text="What this proposal's award draws from it."
    )
    fits = serializers.BooleanField()


class ProposalBudgetSerializer(serializers.Serializer):
    budget_enforcement = serializers.ChoiceField(
        choices=enums.BudgetEnforcement.CHOICES
    )
    entries = ProposalBudgetEntrySerializer(many=True)


class CallProposalFieldConfigSerializer(serializers.ModelSerializer):
    """The per-call Project details field states, as a flat map of field -> state."""

    class Meta:
        model = models.CallProposalFieldConfig
        fields = [
            models.CallProposalFieldConfig.column_for(field_name)
            for field_name in models.CallProposalFieldConfig.field_names()
        ]
        extra_kwargs = {field: {"required": False} for field in fields}


class ProposalFieldMetadataSerializer(serializers.Serializer):
    """What the call configuration UI needs to render one field's row.

    ``usage`` names the consumers that field feeds, so the manager can see what
    switching it off costs; ``allowed_states`` and ``locked_reason`` carry the
    locking rule, so the UI never has to reimplement it.
    """

    field = serializers.CharField()
    state = serializers.ChoiceField(choices=ProposalFieldStates.CHOICES)
    allowed_states = serializers.ListField(child=serializers.CharField())
    locked_reason = serializers.CharField(allow_null=True)
    usage = serializers.ListField(child=serializers.CharField())


def get_proposal_field_metadata(call) -> list[dict]:
    """Per-field state, permitted transitions and consumers for one call.

    A field may not become required once the call has a proposal: tightening
    then invalidates drafts that were complete under the form the applicant was
    shown, and nothing tells them. Loosening stays open in both directions.
    """
    states = models.CallProposalFieldConfig.get_states_for_call(call)
    has_proposals = models.Proposal.objects.filter(round__call=call).exists()
    every_state = [state for state, _label in ProposalFieldStates.CHOICES]
    metadata = []
    for field_name, usage in PROPOSAL_CONFIGURABLE_FIELDS.items():
        current = states[field_name]
        locked = has_proposals and current != ProposalFieldStates.REQUIRED
        metadata.append(
            {
                "field": field_name,
                "state": current,
                "allowed_states": [
                    state
                    for state in every_state
                    if not (locked and state == ProposalFieldStates.REQUIRED)
                ],
                "locked_reason": (
                    "A field cannot be made required once the call has proposals: "
                    "drafts that were complete under the published form would "
                    "silently stop being submittable. Duplicate the call to run a "
                    "stricter round."
                    if locked
                    else None
                ),
                "usage": list(usage),
            }
        )
    return metadata


class CallPanelPersonSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    full_name = serializers.CharField()


class CallPanelDomainSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    code = serializers.CharField()
    name = serializers.CharField()


class PanelMembersField(serializers.ListField):
    """The uuids of a panel's members, read from and written to its M2M."""

    def get_attribute(self, instance):
        return [
            user.uuid
            for user in instance.members.order_by("first_name", "last_name", "id")
        ]


class CallPanelSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """A panel of a call: the domains it covers, its chair and its members.

    Members are panel members of the call, and the chair one of the members.
    Within a call no domain, and no sub-domain, is mapped to two panels; a
    sub-domain mapped to a panel goes there whichever panel its domain maps
    to.
    """

    uuid = serializers.UUIDField(read_only=True, format="hex")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True, format="hex")
    key = serializers.SlugField(
        max_length=64,
        required=False,
        help_text=(
            "Identifier of the panel within its call. Derived from the name "
            "when left out. Cannot change once the panel exists."
        ),
    )
    chair = serializers.UUIDField(
        source="chair.uuid",
        format="hex",
        allow_null=True,
        required=False,
        help_text="The panel's chair, one of its members.",
    )
    chair_name = serializers.ReadOnlyField(source="chair.full_name", allow_null=True)
    members = PanelMembersField(
        child=serializers.UUIDField(format="hex"),
        required=False,
        help_text="Panel members of the call who sit on the panel.",
    )
    member_details = serializers.SerializerMethodField()
    science_domains = serializers.SlugRelatedField(
        slug_field="uuid",
        many=True,
        required=False,
        queryset=structure_models.ScienceDomain.objects.all(),
        help_text="Proposals of these science domains go to the panel.",
    )
    science_sub_domains = serializers.SlugRelatedField(
        slug_field="uuid",
        many=True,
        required=False,
        queryset=structure_models.ScienceSubDomain.objects.all(),
        help_text=(
            "Proposals of these science sub-domains go to the panel, whichever "
            "panel their domain maps to."
        ),
    )
    science_domain_details = serializers.SerializerMethodField()
    science_sub_domain_details = serializers.SerializerMethodField()
    proposals_count = serializers.SerializerMethodField(
        help_text="How many proposals of the call are routed to the panel."
    )

    class Meta:
        model = models.CallPanel
        fields = [
            "uuid",
            "created",
            "modified",
            "call_uuid",
            "key",
            "name",
            "description",
            "order",
            "chair",
            "chair_name",
            "members",
            "member_details",
            "science_domains",
            "science_sub_domains",
            "science_domain_details",
            "science_sub_domain_details",
            "proposals_count",
        ]
        read_only_fields = ("uuid", "created", "modified")
        protected_fields = ("call", "key")
        # (call, key) is checked in validate(): the call is set by the view.
        validators = []

    @extend_schema_field(CallPanelPersonSerializer(many=True))
    def get_member_details(self, panel):
        return [
            {"uuid": user.uuid.hex, "full_name": user.full_name}
            for user in panel.members.order_by("first_name", "last_name", "id")
        ]

    @extend_schema_field(CallPanelDomainSerializer(many=True))
    def get_science_domain_details(self, panel):
        return [
            {"uuid": domain.uuid.hex, "code": domain.code, "name": domain.name}
            for domain in panel.science_domains.all()
        ]

    @extend_schema_field(CallPanelDomainSerializer(many=True))
    def get_science_sub_domain_details(self, panel):
        return [
            {"uuid": sub.uuid.hex, "code": sub.code, "name": sub.name}
            for sub in panel.science_sub_domains.all()
        ]

    def get_proposals_count(self, panel) -> int:
        return panel.proposals.count()

    def _panel_members(self, call, uuids, field):
        """The users ``uuids`` names, each a panel member of ``call``. One
        that is not -- or does not exist -- is refused alike."""
        wanted = {uuid.hex for uuid in uuids}
        found = {
            user.uuid.hex: user
            for user in panels.call_panel_members(call).filter(uuid__in=wanted)
        }
        missing = sorted(wanted - set(found))
        if missing:
            raise serializers.ValidationError(
                {
                    field: _(
                        "Not a panel member of the call: %(users)s. Add them "
                        "in the call's Team tab first."
                    )
                    % {"users": ", ".join(missing)}
                }
            )
        return list(found.values())

    def validate(self, attrs):
        attrs = super().validate(attrs)
        call = attrs.get("call") or getattr(self.instance, "call", None)
        key = attrs.get("key")
        if (
            self.instance is None
            and key
            and call is not None
            and models.CallPanel.objects.filter(call=call, key=key).exists()
        ):
            raise serializers.ValidationError(
                {"key": _("The call already has a panel with this key.")}
            )
        if call is None:
            return attrs
        if "members" in attrs:
            attrs["members"] = self._panel_members(call, attrs["members"], "members")
            member_ids = {user.pk for user in attrs["members"]}
        elif self.instance is not None:
            member_ids = set(self.instance.members.values_list("id", flat=True))
        else:
            member_ids = set()
        if "chair" in attrs:
            chair_uuid = (attrs.pop("chair") or {}).get("uuid")
            chair = None
            if chair_uuid is not None:
                chair = self._panel_members(call, [chair_uuid], "chair")[0]
            attrs["chair"] = chair
        else:
            chair = getattr(self.instance, "chair", None)
        if chair is not None and chair.pk not in member_ids:
            raise serializers.ValidationError(
                {"chair": _("The chair must be one of the panel's members.")}
            )
        others = models.CallPanel.objects.filter(call=call)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        for field, label in (
            ("science_domains", _("domain")),
            ("science_sub_domains", _("sub-domain")),
        ):
            values = attrs.get(field)
            if not values:
                continue
            taken = others.filter(**{f"{field}__in": values}).first()
            if taken is not None:
                raise serializers.ValidationError(
                    {
                        field: _(
                            "A %(what)s can go to one panel only; the panel "
                            "'%(panel)s' already covers one of these."
                        )
                        % {"what": label, "panel": taken.name}
                    }
                )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        members = validated_data.pop("members", [])
        domains = validated_data.pop("science_domains", [])
        sub_domains = validated_data.pop("science_sub_domains", [])
        panel = models.CallPanel.objects.create(**validated_data)
        panel.members.set(members)
        panel.science_domains.set(domains)
        panel.science_sub_domains.set(sub_domains)
        return panel

    @transaction.atomic
    def update(self, instance, validated_data):
        # Locked as revoking the panel member role locks the call's panels,
        # so a member taken off meanwhile is not put back.
        models.CallPanel.objects.select_for_update().filter(pk=instance.pk).first()
        members = validated_data.pop("members", None)
        domains = validated_data.pop("science_domains", None)
        sub_domains = validated_data.pop("science_sub_domains", None)
        instance = super().update(instance, validated_data)
        if members is not None:
            instance.members.set(members)
        if domains is not None:
            instance.science_domains.set(domains)
        if sub_domains is not None:
            instance.science_sub_domains.set(sub_domains)
        return instance


# A call's logo is shown as a small avatar; anything larger only bloats the page.
CALL_IMAGE_MAX_BYTES = 2 * 1024 * 1024


class PublicCallSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    # Read-only here, writable on the protected serializer: the applicant's form
    # renders from this, the call manager configures it.
    proposal_field_config = CallProposalFieldConfigSerializer(read_only=True)
    # Public so the applicant's form can show the project name their proposal
    # name becomes; the call manager sets it on the protected serializer.
    project_name_template = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text=PROJECT_NAME_TEMPLATE_HELP,
    )
    customer_name = serializers.ReadOnlyField(source="manager.customer.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="manager.customer.uuid"
    )
    manager_uuid = serializers.UUIDField(read_only=True, source="manager.uuid")
    offerings = serializers.SerializerMethodField(method_name="get_offerings")
    rounds = serializers.SerializerMethodField()
    start_date = serializers.SerializerMethodField()
    end_date = serializers.SerializerMethodField()
    documents = CallDocumentSerializer(many=True, read_only=True)
    resource_templates = serializers.SerializerMethodField()
    component_rules = serializers.SerializerMethodField(
        help_text=(
            "What a proposal may request of each component of the call's "
            "accepted offerings, in the component's unit and per its limit "
            "period."
        ),
    )
    fixed_duration_in_days = serializers.ReadOnlyField()
    allowed_duration_months = serializers.ListField(
        child=serializers.IntegerField(),
        read_only=True,
        help_text=(
            "Durations, in months, the applicant chooses the project's length "
            "from; empty when the call offers no choice."
        ),
    )
    max_prepaid_duration_months = serializers.SerializerMethodField(
        help_text="The longest prepaid subscription, in whole months, that fits "
        "inside fixed_duration_in_days measured from today; null when the call "
        "fixes no duration."
    )
    description = core_serializers.HTMLCleanField(
        required=False, allow_blank=True, max_length=DESCRIPTION_LENGTH
    )
    has_eligibility_restrictions = serializers.SerializerMethodField()
    image = serializers.ImageField(read_only=True, allow_null=True)

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_max_prepaid_duration_months(self, call) -> int | None:
        # Anchored on today: a call has many rounds, and the applicant reads
        # this before choosing one. The request itself is validated against
        # the round's own allocation date.
        return utils.max_prepaid_duration_months(call, datetime.today().date())

    class Meta:
        model = models.Call
        fields = (
            "url",
            "uuid",
            "created",
            "start_date",
            "end_date",
            "slug",
            "name",
            "description",
            "image",
            "state",
            "manager",
            "manager_uuid",
            "customer_name",
            "customer_uuid",
            "offerings",
            "rounds",
            "documents",
            "resource_templates",
            "component_rules",
            "fixed_duration_in_days",
            "allowed_duration_months",
            "max_prepaid_duration_months",
            "backend_id",
            "external_url",
            "reviewer_identity_visible_to_submitters",
            "reviews_visible_to_submitters",
            "has_eligibility_restrictions",
            "proposal_field_config",
            # Public so an applicant can be told their evaluation waits for
            # the cut-off.
            "evaluation_start",
            # Public so an applicant can be told results come with the round.
            "publish_results",
            # Public so the applicant's form offers one template or several.
            "resource_template_selection_mode",
            "project_name_template",
        )
        view_name = "proposal-public-call-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "manager": {
                "lookup_field": "uuid",
                "view_name": "call-managing-organisation-detail",
            },
            "created_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "documents": {"required": False},
            "reviewer_identity_visible_to_submitters": {"required": False},
            "reviews_visible_to_submitters": {
                "required": False,
                "help_text": "Whether proposal applicants can see review comments "
                "and scores. Applicants still see what an applicant step lists "
                "as its input steps, such as the reviews they respond to.",
            },
        }

    def get_start_date(self, obj) -> datetime:
        first_round = obj.round_set.order_by("start_time").first()
        return first_round.start_time if first_round else None

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_end_date(self, obj) -> datetime | None:
        """The last cut-off; null while a round has none (open until closed)."""
        cutoffs = [r.cutoff_time for r in obj.round_set.all()]
        if None in cutoffs:
            return None
        return max(cutoffs, default=None)

    @extend_schema_field(NestedRequestedOfferingSerializer(many=True))
    def get_offerings(self, obj):
        queryset = obj.requestedoffering_set.filter(
            state=RequestedOfferingStates.ACCEPTED
        )
        serializer = NestedRequestedOfferingSerializer(
            queryset,
            many=True,
            read_only=True,
            context=self.context,
        )
        return serializer.data

    @extend_schema_field(NestedRoundSerializer(many=True))
    def get_rounds(self, obj):
        queryset = obj.round_set.all()
        now = timezone.now()
        all_open_rounds = queryset.filter(round_open_q(now=now))

        all_scheduled_rounds = queryset.filter(Q(start_time__gt=now))

        all_closed_rounds = queryset.filter(Q(cutoff_time__lt=now))

        sorted_queryset = (
            list(all_open_rounds) + list(all_scheduled_rounds) + list(all_closed_rounds)
        )
        serializer = NestedRoundSerializer(
            sorted_queryset,
            many=True,
            read_only=True,
            context=self.context,
        )
        return serializer.data

    @extend_schema_field(CallResourceTemplateSerializer(many=True))
    def get_resource_templates(self, obj):
        queryset = obj.resource_templates.all()
        serializer = CallResourceTemplateSerializer(
            queryset,
            many=True,
            read_only=True,
            context=self.context,
        )
        return serializer.data

    @extend_schema_field(CallOfferingComponentBoundsSerializer(many=True))
    def get_component_rules(self, obj):
        rules = models.CallOfferingComponent.objects.filter(
            requested_offering__call=obj,
            requested_offering__state=RequestedOfferingStates.ACCEPTED,
        ).select_related("requested_offering__offering", "requested_offering__plan")
        return CallOfferingComponentBoundsSerializer(
            rules, many=True, context=self.context
        ).data

    def get_has_eligibility_restrictions(self, obj) -> bool:
        """Check if call has any eligibility restrictions configured."""
        return bool(
            obj.user_nationalities
            or obj.user_organization_types
            or obj.user_assurance_levels
            or obj.user_email_patterns
            or obj.user_affiliations
            or obj.user_identity_sources
            or obj.manager.max_active_proposals_per_applicant is not None
        )


class RequestedOfferingSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    NestedRequestedOfferingSerializer,
):
    url = serializers.SerializerMethodField()
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    approved_by_name = serializers.ReadOnlyField(source="approved_by.full_name")
    component_rules = serializers.SerializerMethodField(
        help_text="What a proposal may request of each component of the offering."
    )
    requestable_components = serializers.SerializerMethodField(
        help_text=(
            "The components a rule can be set for: billed by their limit and "
            "priced by the call's plan."
        )
    )
    rules_locked = serializers.SerializerMethodField(
        help_text=(
            "Whether a submitted proposal requests the offering: its component "
            "rules can no longer change."
        )
    )

    class Meta(NestedRequestedOfferingSerializer.Meta):
        fields = NestedRequestedOfferingSerializer.Meta.fields + [
            "url",
            "approved_by",
            "created_by",
            "created_by_name",
            "approved_by_name",
            "description",
            "component_rules",
            "requestable_components",
            "rules_locked",
        ]
        read_only_fields = (
            "created_by",
            "approved_by",
            # The requirement belongs to the offering's provider, whose flag
            # gates order approval; the call entry only decides what the
            # proposal collects, and is seeded from that flag when the offering
            # is added. Writing it here let a call manager lower a provider's
            # requirement — before the provider had even accepted the entry, and
            # without either of them being shown the field anywhere.
            "require_purchase_order",
        )
        protected_fields = ("offering",)
        extra_kwargs = {
            **NestedRequestedOfferingSerializer.Meta.extra_kwargs,
            **{
                "approved_by": {
                    "lookup_field": "uuid",
                    "view_name": "user-detail",
                },
                "created_by": {
                    "lookup_field": "uuid",
                    "view_name": "user-detail",
                },
            },
        }

    def get_url(self, requested_offering) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-offering-detail",
                kwargs={
                    "uuid": requested_offering.call.uuid.hex,
                    "obj_uuid": requested_offering.uuid.hex,
                },
            )
        )

    def validate_offering(self, offering):
        user = self.context["request"].user

        if not (
            marketplace_models.Offering.objects.filter(id=offering.id)
            .filter_by_ordering_availability_for_user(user)
            .exists()
        ):
            raise serializers.ValidationError(
                {"offering": _("You do not have permissions for this offering.")}
            )

        return offering

    @extend_schema_field(CallOfferingComponentSerializer(many=True))
    def get_component_rules(self, requested_offering):
        return CallOfferingComponentSerializer(
            requested_offering.component_rules.all(),
            many=True,
            context=self.context,
        ).data

    @extend_schema_field(RequestableComponentSerializer(many=True))
    def get_requestable_components(self, requested_offering):
        return RequestableComponentSerializer(
            requested_offering.requestable_components().values(), many=True
        ).data

    def get_rules_locked(self, requested_offering) -> bool:
        return budgets.rules_locked(requested_offering)

    def validate_attributes(self, attributes):
        if not attributes:
            return {}
        if "limits" in attributes:
            raise serializers.ValidationError(
                _(
                    "Set what a proposal may request of each component in the "
                    "offering's component rules."
                )
            )
        return attributes

    def validate(self, attrs):
        # A rule names a component the plan prices: a plan that does not
        # would leave the rule bounding nothing.
        plan = attrs.get("plan")
        if self.instance is not None and "plan" in attrs and plan != self.instance.plan:
            probe = models.RequestedOffering(offering=self.instance.offering, plan=plan)
            requestable = probe.requestable_components()
            orphaned = sorted(
                rule.component_type
                for rule in self.instance.component_rules.all()
                if rule.component_type not in requestable
            )
            if orphaned:
                raise serializers.ValidationError(
                    {
                        "plan": _(
                            "The plan does not price the components the call's "
                            "rules are set for: %s. Remove those rules first."
                        )
                        % ", ".join(orphaned)
                    }
                )
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


PREPAID_DURATION_FIELD = "attributes.prepaid_duration_months"


def _validate_required_options(attributes, offering):
    """Refuse values that leave a required option of offering empty."""
    missing = models.missing_required_options(offering, attributes)
    if missing:
        raise serializers.ValidationError(
            {
                "attributes": models.REQUIRED_OPTIONS_MISSING_FIELD_MESSAGE
                % {"options": ", ".join(missing)}
            }
        )


def _validate_prepaid_duration(
    attributes, offering, proposal_round, chosen_months=None
):
    """Hold the requested subscription length to the terms it is sold under.

    The offering's prepaid min/max/step, as the marketplace order path applies
    them, and the call's fixed duration: that is the length of every project
    the call awards, so no subscription requested under it may outlast it
    (``utils.max_prepaid_duration_months``), measured from the day allocation
    is scheduled for, or today where the call allocates on decision. A length
    chosen from the call's list (``chosen_months``) caps it the same way.
    """
    attributes = attributes or {}
    if "prepaid_duration_months" not in attributes:
        return

    months = attributes["prepaid_duration_months"]
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise serializers.ValidationError(
            {PREPAID_DURATION_FIELD: _("A whole number of months, at least 1.")}
        )

    prepaid_components = list(offering.components.filter(is_prepaid=True))
    if not prepaid_components:
        raise serializers.ValidationError(
            {PREPAID_DURATION_FIELD: _("This offering is not sold by the month.")}
        )
    for component in prepaid_components:
        validate_prepaid_duration_against_component(
            months, component, PREPAID_DURATION_FIELD
        )

    if proposal_round is None:
        # A call-level check, as for a template: the cap depends on the round.
        return
    anchor = utils.allocation_start_date(proposal_round) or datetime.today().date()
    cap = utils.max_prepaid_duration_months(proposal_round.call, anchor, chosen_months)
    if (
        cap is not None
        and months > cap
        and not proposal_round.call.fixed_duration_in_days
    ):
        raise serializers.ValidationError(
            {
                PREPAID_DURATION_FIELD: _(
                    "The project runs for %(cap)s month(s), so a subscription "
                    "may run for at most that long."
                )
                % {"cap": cap}
            }
        )
    if cap is not None and months > cap:
        raise serializers.ValidationError(
            {
                PREPAID_DURATION_FIELD: _(
                    "This call awards projects of %(days)s days, so a subscription "
                    "may run for at most %(cap)s month(s) from %(anchor)s."
                )
                % {
                    "days": proposal_round.call.fixed_duration_in_days,
                    "cap": cap,
                    "anchor": anchor.isoformat(),
                }
            }
        )


def requested_duration_cap(proposal):
    """The length the applicant chose from the call's list, which caps the
    subscriptions they request; None where the call offers no choice."""
    if not proposal.round.call.allowed_duration_months:
        return None
    return proposal.requested_duration_months


def awarded_duration_cap(proposal):
    """The awarded length from the call's list, which caps the awarded
    subscriptions; None where the call offers no choice."""
    if not proposal.round.call.allowed_duration_months:
        return None
    return proposal.awarded_duration_months


def prepaid_months_above(items, cap):
    """Names of the offerings of ``items`` whose subscription outlasts ``cap``
    months, for the message refusing a shorter project."""
    return sorted(
        {
            item.requested_offering.offering.name
            for item in items
            if (months := utils.requested_months(item)) is not None and months > cap
        }
    )


class AllocationOrderFieldsMixin(serializers.Serializer):
    """How the order that provisions an allocated item went.

    Allocation creates a resource and its order together, so the resource is
    there from the start whatever becomes of the order: waiting for the
    provider, provisioning, refused or failed. The outcome needs to say which,
    including to an applicant who holds no role on the created project and so
    cannot read the order itself.

    The state follows the item: whoever may read the item may read it. The
    provider's reason for refusing follows the order: only those who may read
    the order see it.
    """

    order_state = serializers.SerializerMethodField(
        help_text=(
            "State of the order that provisions the item's resource; null "
            "while nothing has been provisioned for it."
        )
    )
    order_rejection_comment = serializers.SerializerMethodField(
        help_text=(
            "Why the order was refused, for a viewer who may read the order; "
            "null otherwise."
        )
    )

    def _allocation_order(self, item) -> marketplace_models.Order | None:
        # Both fields read the same order; a list reuses one child serializer,
        # so the lookup is cached per resource on it.
        resource_id = item.resource_id
        if resource_id is None:
            return None
        cache = self.__dict__.setdefault("_allocation_orders", {})
        if resource_id not in cache:
            cache[resource_id] = (
                marketplace_models.Order.objects.filter(
                    resource_id=resource_id, type=OrderTypes.CREATE
                )
                .order_by("-created", "-id")
                .first()
            )
        return cache[resource_id]

    @extend_schema_field(
        serializers.ChoiceField(
            choices=[name for _, name in OrderStates.CHOICES],
            allow_null=True,
            read_only=True,
        )
    )
    def get_order_state(self, item) -> str | None:
        order = self._allocation_order(item)
        return order.get_state_display() if order else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_order_rejection_comment(self, item) -> str | None:
        order = self._allocation_order(item)
        if order is None or order.state != OrderStates.REJECTED:
            return None
        request = self.context.get("request")
        if request is None or not (
            filter_orders_for_user(
                marketplace_models.Order.objects.filter(pk=order.pk), request.user
            ).exists()
        ):
            return None
        return (
            order.provider_rejection_comment or order.consumer_rejection_comment or None
        )


class RequestedResourceSerializer(
    core_serializers.AugmentedSerializerMixin,
    AllocationOrderFieldsMixin,
    NestedRequestedResourceSerializer,
):
    requested_offering_uuid = serializers.UUIDField(write_only=True, required=False)
    call_resource_template_uuid = serializers.UUIDField(write_only=True, required=False)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The request's creator is a member of the applicant team: an
        # evaluator sees them only as far as the call's config exposes them.
        request = self.context.get("request")
        if request is not None:
            filter_applicant_fields_for_reviewer(
                data,
                instance.proposal,
                request.user,
                get_applicant_visibility(self.context, request.user),
            )
        return data

    class Meta(NestedRequestedResourceSerializer.Meta):
        fields = NestedRequestedResourceSerializer.Meta.fields + [
            "requested_offering_uuid",
            "call_resource_template_uuid",
            "order_state",
            "order_rejection_comment",
        ]

        read_only_fields = (
            "created_by",
            "resource",
        )

    @staticmethod
    def _bound_limits(attrs, proposal, requested_offering):
        """Hold the limits to the offering's component rules; a fixed amount
        is set, whatever was sent."""
        try:
            attrs["limits"] = budgets.bound_limits(
                requested_offering, attrs.get("limits")
            )
        except ValueError as exc:
            raise serializers.ValidationError({"limits": str(exc)})

    def validate(self, attrs):
        if self.instance:
            offering = self.instance.requested_offering.offering
            if "attributes" in attrs:
                attrs["attributes"] = utils.normalize_option_values(
                    attrs["attributes"], offering
                )
            # The template sets these; changing them would make the request a
            # custom configuration, which a template call does not accept.
            # Compared after normalising, as the stored values were.
            if self.instance.call_resource_template_id:
                for field in ("limits", "attributes"):
                    if field in attrs and attrs[field] != getattr(self.instance, field):
                        raise serializers.ValidationError(
                            {field: models.TEMPLATE_VALUES_LOCKED_MESSAGE}
                        )
            if "attributes" in attrs:
                _validate_prepaid_duration(
                    attrs["attributes"],
                    self.instance.requested_offering.offering,
                    self.instance.proposal.round,
                    requested_duration_cap(self.instance.proposal),
                )
            if "limits" in attrs:
                self._bound_limits(
                    attrs, self.instance.proposal, self.instance.requested_offering
                )
            return attrs

        proposal = attrs["proposal"]
        call = proposal.round.call

        # Handle resource template based requests
        call_resource_template_uuid = attrs.pop("call_resource_template_uuid", None)
        requested_offering_uuid = attrs.pop("requested_offering_uuid", None)

        if call_resource_template_uuid:
            # Creating from template
            try:
                template = call.resource_templates.get(uuid=call_resource_template_uuid)
            except models.CallResourceTemplate.DoesNotExist:
                raise serializers.ValidationError(
                    {"call_resource_template_uuid": _("Resource template not found.")}
                )

            if template.requested_offering.state != RequestedOfferingStates.ACCEPTED:
                raise serializers.ValidationError(
                    _("Offering has not been confirmed by service provider.")
                )
            # A request made from a template carries the template's values;
            # values the client sends are compared as they would be stored.
            offering = template.requested_offering.offering
            limits, attributes = utils.template_request_values(template)
            if attrs.get("limits") and attrs["limits"] != limits:
                raise serializers.ValidationError(
                    {"limits": models.TEMPLATE_VALUES_LOCKED_MESSAGE}
                )
            if (
                attrs.get("attributes")
                and utils.normalize_option_values(attrs["attributes"], offering)
                != attributes
            ):
                raise serializers.ValidationError(
                    {"attributes": models.TEMPLATE_VALUES_LOCKED_MESSAGE}
                )
            attrs["call_resource_template"] = template
            attrs["requested_offering"] = template.requested_offering
            attrs["limits"] = limits
            attrs["attributes"] = attributes
            _validate_prepaid_duration(attributes, offering, proposal.round)
            return attrs

        elif requested_offering_uuid:
            # Once a call has templates, they are the only way to request.
            if call.resource_templates.exists():
                raise serializers.ValidationError(
                    {
                        "requested_offering_uuid": _(
                            "This call only accepts its resource templates. "
                            "Choose one of them instead."
                        )
                    }
                )
            try:
                requested_offering = call.requestedoffering_set.get(
                    uuid=requested_offering_uuid
                )
            except models.RequestedOffering.DoesNotExist:
                raise serializers.ValidationError(
                    {
                        "requested_offering_uuid": _(
                            "Requested offering has not been found."
                        )
                    }
                )

            if requested_offering.state != RequestedOfferingStates.ACCEPTED:
                raise serializers.ValidationError(
                    _("Offering has not been confirmed by service provider.")
                )

            attrs["requested_offering"] = requested_offering
        else:
            raise serializers.ValidationError(
                _(
                    "Either requested_offering_uuid or call_resource_template_uuid must be provided."
                )
            )

        offering = attrs["requested_offering"].offering
        if attrs.get("attributes"):
            attrs["attributes"] = utils.normalize_option_values(
                attrs["attributes"], offering
            )
        _validate_prepaid_duration(
            attrs.get("attributes"),
            offering,
            proposal.round,
            requested_duration_cap(proposal),
        )
        self._bound_limits(attrs, proposal, attrs["requested_offering"])
        return attrs

    def validate_attributes(self, attributes):
        if not attributes:
            return {}

        return attributes

    def validate_proposal(self, proposal):
        if not change_requests.applicant_may_edit(
            proposal, ChangeRequestParts.RESOURCES
        ):
            raise serializers.ValidationError(change_requests.LOCKED_MESSAGE)
        if proposal.state != ProposalStates.DRAFT and (
            change_requests.has_offering_slots(proposal)
        ):
            raise serializers.ValidationError(change_requests.OFFERINGS_FIXED_MESSAGE)
        return proposal

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        proposal = validated_data["proposal"]
        with transaction.atomic():
            # The call row is the lock both this check and a switch to single
            # mode take, so neither a concurrent request nor the switch can
            # slip in between the check and the insert. no_key leaves inserts
            # that reference the call (rounds, templates) unblocked.
            call = models.Call.objects.select_for_update(no_key=True).get(
                pk=proposal.round.call_id
            )
            # validate_proposal ran before any lock; submit locks this row, so
            # the draft check is only reliable once we hold it too.
            proposal = models.Proposal.objects.select_for_update(no_key=True).get(
                pk=proposal.pk
            )
            if proposal.state != ProposalStates.DRAFT:
                raise serializers.ValidationError(
                    _("Only proposals with a draft status are available for editing.")
                )
            template = validated_data.get("call_resource_template")
            if (
                template
                and proposal.requestedresource_set.filter(
                    call_resource_template=template
                ).exists()
            ):
                raise serializers.ValidationError(
                    {
                        "call_resource_template_uuid": _(
                            "This template is already requested on this proposal."
                        )
                    }
                )
            if (
                call.allows_one_request_only()
                and proposal.requestedresource_set.exists()
            ):
                raise serializers.ValidationError(
                    _(
                        "This call allows only one resource template per "
                        "proposal. Remove the current resource request first."
                    )
                )
            return super().create(validated_data)


class RequestedResourcePurchaseOrderSerializer(serializers.ModelSerializer):
    """Multipart write of the purchase order, mirroring OrderAttachmentSerializer.

    Kept off the main serializer because a file cannot ride along with the JSON
    body the resource form submits.
    """

    class Meta:
        model = models.RequestedResource
        fields = ("attachment", "purchase_order_reference")
        extra_kwargs = {
            "attachment": {"required": False, "allow_null": True},
            "purchase_order_reference": {"required": False, "allow_blank": True},
        }


def _validate_awarded_limits(limits, offering, plan):
    """Hold awarded limits to what the offering's components accept.

    The keys must be the limit components of the plan (or of the offering
    where there is no plan), and each value inside the component's bounds and
    precision -- the checks every order placing these limits passes. The
    running total against ``max_available_limit`` is left to the order that
    allocation places, as it is for a requested amount.
    """
    try:
        for component, value in marketplace_utils.get_components_map(
            limits, offering, plan
        ):
            marketplace_utils.validate_limit_precision(value, component)
            marketplace_utils.validate_min_max_limit(value, component)
    except serializers.ValidationError as exc:
        detail = exc.detail
        if isinstance(detail, dict) and "limits" in detail:
            detail = detail["limits"]
        raise serializers.ValidationError({"limits": detail})


class AwardedResourceSerializer(
    AllocationOrderFieldsMixin, serializers.ModelSerializer
):
    """An item of the award the allocation decision grants.

    Written by the call manager while the allocation decision is active: the
    amounts, the call offering it is awarded on, its plan and attributes.
    ``requested_resource`` names the request the item was prefilled from and is
    empty for an item the call manager added.
    """

    url = serializers.SerializerMethodField()
    requested_offering = NestedRequestedOfferingSerializer(read_only=True)
    requested_offering_uuid = serializers.UUIDField(
        write_only=True,
        required=False,
        help_text=(
            "An accepted offering of the proposal's call to award the item on. "
            "Required when adding an item."
        ),
    )
    requested_resource = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    plan = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=marketplace_models.Plan.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "Plan of the awarded offering. Read back as the plan the item is "
            "provisioned on: the call offering's plan unless another was set. "
            "Null (or leaving it out when adding or moving) follows the call "
            "offering's plan."
        ),
    )
    plan_name = serializers.ReadOnlyField(source="effective_plan.name", allow_null=True)
    resource = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    resource_name = serializers.ReadOnlyField(source="resource.name", allow_null=True)
    created_by = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    created_by_name = serializers.ReadOnlyField(
        source="created_by.full_name", allow_null=True
    )
    modified_by_name = serializers.ReadOnlyField(
        source="modified_by.full_name",
        allow_null=True,
        help_text="Who last changed the item; null if nobody did since it was made.",
    )
    limits = serializers.DictField(child=LimitValueField(), required=False)
    attributes = serializers.JSONField(required=False)

    class Meta:
        model = models.AwardedResource
        fields = [
            "uuid",
            "url",
            "requested_resource",
            "requested_offering",
            "requested_offering_uuid",
            "plan",
            "plan_name",
            "attributes",
            "limits",
            "description",
            "resource",
            "resource_name",
            "order_state",
            "order_rejection_comment",
            "provision_error",
            "created_by",
            "created_by_name",
            "modified_by_name",
            "created",
            "modified",
        ]
        read_only_fields = ("created", "modified", "provision_error")

    def get_url(self, awarded) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-proposal-awarded_resource-detail",
                kwargs={
                    "uuid": awarded.proposal.uuid.hex,
                    "obj_uuid": awarded.uuid.hex,
                },
            )
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # What allocation provisions, not only an explicit choice.
        plan = instance.effective_plan
        data["plan"] = (
            None if plan is None else self.fields["plan"].to_representation(plan)
        )
        return data

    def validate_attributes(self, attributes):
        if not attributes:
            return {}
        if not isinstance(attributes, dict):
            raise serializers.ValidationError(_("A JSON object is expected."))
        return attributes

    def validate(self, attrs):
        proposal = self.instance.proposal if self.instance else attrs["proposal"]
        call = proposal.round.call

        requested_offering_uuid = attrs.pop("requested_offering_uuid", None)
        moved = False
        if requested_offering_uuid is not None:
            try:
                requested_offering = call.requestedoffering_set.select_related(
                    "offering", "plan"
                ).get(uuid=requested_offering_uuid)
            except models.RequestedOffering.DoesNotExist:
                raise serializers.ValidationError(
                    {
                        "requested_offering_uuid": _(
                            "The offering is not part of this call."
                        )
                    }
                )
            if requested_offering.state != RequestedOfferingStates.ACCEPTED:
                raise serializers.ValidationError(
                    {
                        "requested_offering_uuid": _(
                            "The offering has not been accepted by its provider."
                        )
                    }
                )
            moved = (
                self.instance is None
                or self.instance.requested_offering_id != requested_offering.id
            )
            attrs["requested_offering"] = requested_offering
        elif self.instance is None:
            raise serializers.ValidationError(
                {"requested_offering_uuid": _("This field is required.")}
            )
        else:
            requested_offering = self.instance.requested_offering

        offering = requested_offering.offering

        if "plan" in attrs:
            plan = attrs["plan"]
            if plan is not None and plan.offering_id != offering.id:
                raise serializers.ValidationError(
                    {"plan": _("The plan does not belong to the awarded offering.")}
                )
            if plan is not None and plan.archived:
                raise serializers.ValidationError({"plan": _("The plan is archived.")})
        elif moved:
            # The old plan belongs to another offering: follow the call's.
            plan = attrs["plan"] = None
        else:
            plan = self.instance.plan if self.instance else None
        # An item without a plan of its own is provisioned on the call's.
        effective_plan = plan or requested_offering.plan

        limits = (
            attrs["limits"]
            if "limits" in attrs
            else (self.instance.limits if self.instance else {})
        )
        # The rule a proposal is submitted under: an amount must be named for
        # an offering that sells by amount, or allocation provisions nothing.
        if not models.names_an_amount(offering, limits):
            raise serializers.ValidationError(
                {"limits": _("An amount must be awarded for this offering.")}
            )
        if "limits" in attrs or moved or "plan" in attrs:
            _validate_awarded_limits(limits or {}, offering, effective_plan)

        attributes = (
            attrs["attributes"]
            if "attributes" in attrs
            else (self.instance.attributes if self.instance else {})
        )
        if "attributes" in attrs or moved:
            attributes = utils.normalize_option_values(attributes, offering)
            if "attributes" in attrs:
                attrs["attributes"] = attributes
            _validate_prepaid_duration(
                attributes, offering, proposal.round, awarded_duration_cap(proposal)
            )
        # On every save, as for amounts above: the decision refuses an award
        # leaving a required option empty, so the editor says so first.
        _validate_required_options(attributes, offering)

        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        item = super().create(validated_data)
        activity.log_award_changed(item.proposal, "added", item)
        return item

    def update(self, instance, validated_data):
        before = activity.award_item_state(instance)
        validated_data["modified_by"] = self.context["request"].user
        item = super().update(instance, validated_data)
        if activity.award_item_state(item) != before:
            activity.log_award_changed(item.proposal, "changed", item, before)
        return item


class ProviderRequestedResourceSerializer(NestedRequestedResourceSerializer):
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(
        source="proposal.uuid", read_only=True, format="hex"
    )
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    proposal_state = serializers.ChoiceField(
        source="proposal.state", choices=ProposalStates.CHOICES, read_only=True
    )
    call_name = serializers.ReadOnlyField(source="proposal.round.call.name")
    call_uuid = serializers.UUIDField(
        source="proposal.round.call.uuid", read_only=True, format="hex"
    )
    round_name = serializers.ReadOnlyField(source="proposal.round.name")
    offering_name = serializers.ReadOnlyField(source="requested_offering.offering.name")
    offering_uuid = serializers.UUIDField(
        source="requested_offering.offering.uuid", read_only=True, format="hex"
    )
    # Null until the proposal is awarded and the resource provisioned.
    resource_uuid = serializers.UUIDField(
        source="resource.uuid", read_only=True, allow_null=True, format="hex"
    )
    resource_state = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_resource_state(self, requested_resource) -> str | None:
        if not requested_resource.resource:
            return None
        return requested_resource.resource.get_state_display()

    class Meta(NestedRequestedResourceSerializer.Meta):
        fields = NestedRequestedResourceSerializer.Meta.fields + [
            "proposal_name",
            "proposal",
            "proposal_uuid",
            "proposal_slug",
            "proposal_state",
            "call_name",
            "call_uuid",
            "round_name",
            "offering_name",
            "offering_uuid",
            "resource_uuid",
            "resource_state",
        ]

        extra_kwargs = {
            **NestedRequestedResourceSerializer.Meta.extra_kwargs,
            **{
                "proposal": {
                    "lookup_field": "uuid",
                    "view_name": "proposal-proposal-detail",
                },
                "url": {
                    "lookup_field": "uuid",
                    "view_name": "proposal-requested-resource-detail",
                },
            },
        }


class CallPoolSummarySerializer(serializers.Serializer):
    component_type = serializers.CharField(source="offering_component.component_type")
    round_name = serializers.CharField(
        source="round.name", allow_null=True, default=None
    )
    offered_total = LimitBoundField(read_only=True)


class ProviderRequestedOfferingSerializer(NestedRequestedOfferingSerializer):
    """A request to include one of the provider's offerings in a call, with
    where the call stands and what it asks of the offering's team."""

    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True, format="hex")
    call_state = serializers.ChoiceField(
        source="call.state", choices=CallStates.CHOICES, read_only=True
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_email = serializers.ReadOnlyField(source="created_by.email")
    current_round = serializers.SerializerMethodField(
        help_text=(
            "The call's round that matters now: the open one, else the next "
            "to open, else the one that ended last."
        )
    )
    proposals_count = serializers.SerializerMethodField(
        help_text="Proposals, past their draft, requesting the offering in this call."
    )
    offering_managers_count = serializers.SerializerMethodField(
        help_text=(
            "People holding the offering manager role on the offering: the "
            "team the call's offering-team steps hand work to."
        )
    )
    team_steps_count = serializers.SerializerMethodField(
        help_text=(
            "Enabled steps of the call's workflow the offering's team "
            "completes or contributes to."
        )
    )
    component_rules = CallOfferingComponentBoundsSerializer(
        many=True,
        read_only=True,
        help_text=(
            "What a proposal may request of each component: what the provider "
            "commits to by accepting."
        ),
    )
    pools = serializers.SerializerMethodField(
        help_text="How much of each component the call awards, overall or per round."
    )
    default_delegate_uuid = serializers.UUIDField(
        source="default_delegate.uuid", read_only=True, allow_null=True
    )
    default_delegate_name = serializers.ReadOnlyField(
        source="default_delegate.full_name", allow_null=True
    )

    class Meta(NestedRequestedOfferingSerializer.Meta):
        fields = NestedRequestedOfferingSerializer.Meta.fields + [
            "url",
            "call_name",
            "call_uuid",
            "call_state",
            "call",
            "current_round",
            "proposals_count",
            "offering_managers_count",
            "team_steps_count",
            "description",
            "created_by_name",
            "created_by_email",
            "component_rules",
            "pools",
            "rejection_reason",
            "default_delegate_uuid",
            "default_delegate_name",
        ]
        read_only_fields = ("description", "created_by")
        extra_kwargs = {
            "approved_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "created_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-public-call-detail",
            },
            "plan": {
                "lookup_field": "uuid",
                "view_name": "marketplace-plan-detail",
                "read_only": True,
            },
            "url": {
                "lookup_field": "uuid",
                "view_name": "proposal-requested-offering-detail",
            },
        }

    @extend_schema_field(NestedRoundSerializer(allow_null=True))
    def get_current_round(self, requested_offering):
        call_round = provider_calls.current_round(
            requested_offering.call.round_set.all()
        )
        if call_round is None:
            return None
        return NestedRoundSerializer(call_round, context=self.context).data

    def _counted(self, requested_offering, name) -> int:
        value = getattr(requested_offering, name, None)
        if value is not None:
            return value
        annotated = provider_calls.annotate_requested_offerings(
            models.RequestedOffering.objects.filter(pk=requested_offering.pk)
        ).values_list(name, flat=True)
        return annotated.first() or 0

    def get_proposals_count(self, requested_offering) -> int:
        return self._counted(requested_offering, "proposals_count")

    def get_offering_managers_count(self, requested_offering) -> int:
        return self._counted(requested_offering, "offering_managers_count")

    def get_team_steps_count(self, requested_offering) -> int:
        return self._counted(requested_offering, "team_steps_count")

    @extend_schema_field(CallPoolSummarySerializer(many=True))
    def get_pools(self, requested_offering):
        pools = models.CallResourcePool.objects.filter(
            offering_component__requested_offering=requested_offering
        ).select_related("offering_component", "round")
        return CallPoolSummarySerializer(pools, many=True).data


class OfferingTeamPersonSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    full_name = serializers.CharField()
    email = serializers.CharField(allow_blank=True)
    role_name = serializers.CharField()
    role_description = serializers.CharField(allow_blank=True)


class OfferingTeamSerializer(serializers.Serializer):
    managers = OfferingTeamPersonSerializer(
        many=True,
        help_text="The offering's managers: they do the team's part of each step.",
    )
    delegates = OfferingTeamPersonSerializer(
        many=True,
        help_text=(
            "Everyone else with a role on the offering: a slot of a step that "
            "may be handed within the team can be handed to them."
        ),
    )


class OfferingTeamSlotSerializer(serializers.Serializer):
    """One open slot of the offering's team on a proposal."""

    uuid = serializers.UUIDField(format="hex")
    proposal_uuid = serializers.UUIDField(format="hex")
    proposal_name = serializers.CharField()
    proposal_slug = serializers.CharField(allow_blank=True)
    call_name = serializers.CharField()
    offering_name = serializers.CharField()
    requested_offering_uuid = serializers.UUIDField(format="hex")
    step_name = serializers.CharField()
    step_key = serializers.CharField()
    phase = serializers.ChoiceField(
        choices=WorkflowPhases.CHOICES,
        help_text=(
            "The phase the step runs in: the evaluation before the award, or "
            "an extension or closing after it."
        ),
    )
    status = serializers.ChoiceField(choices=StepContributionStatuses.CHOICES)
    delegate_uuid = serializers.UUIDField(format="hex", allow_null=True)
    delegate_name = serializers.CharField(allow_null=True)
    delegate_accepted = serializers.BooleanField(
        help_text="The person it was handed to has taken it over."
    )
    stand_in = serializers.BooleanField(
        help_text=(
            "Nobody is on the offering's team, so the call's managers stand in for it."
        )
    )
    due_at = serializers.DateTimeField(allow_null=True)
    overdue = serializers.BooleanField()


class DelegateTeamSlotsSerializer(serializers.Serializer):
    contribution_uuids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=False
    )
    user_uuid = serializers.UUIDField(
        allow_null=True, help_text="Who takes the slots over; null takes them back."
    )


class DelegateTeamSlotsResultSerializer(serializers.Serializer):
    handed_on = serializers.ListField(child=serializers.UUIDField(format="hex"))
    skipped = serializers.DictField(
        child=serializers.CharField(),
        help_text="Slots left as they were, with why, by uuid.",
    )


class SetDefaultDelegateSerializer(serializers.Serializer):
    user_uuid = serializers.UUIDField(
        allow_null=True,
        help_text="Who new slots are handed to as they open; null for nobody.",
    )


class RejectRequestedOfferingSerializer(serializers.Serializer):
    reason = serializers.CharField(
        help_text="Why the offering does not take part, for the call's managers."
    )


class OfferingTeamStepSerializer(serializers.Serializer):
    """A step of a call's workflow the team of one of its offerings acts in.

    Only what the team needs to know to do its part: the call's other steps
    and settings stay with the call's managers."""

    uuid = serializers.UUIDField(format="hex")
    key = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    kind = serializers.ChoiceField(choices=StepKinds.CHOICES)
    phase = serializers.ChoiceField(choices=WorkflowPhases.CHOICES)
    order = serializers.IntegerField()
    duration_in_days = serializers.IntegerField(allow_null=True)
    team_part = serializers.ChoiceField(
        choices=provider_calls.TeamParts.CHOICES,
        help_text="Whether the offering managers complete the step or contribute to it.",
    )
    per_requested_offering = serializers.BooleanField(
        help_text="One contribution per requested offering."
    )
    delegable_within_team = serializers.BooleanField(
        help_text="A slot of the step can be handed on to anyone on the offering's team."
    )
    notification_triggers = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "Events of the step the call mails the offering's team about, as "
            "notification rule triggers (step_started, deadline_approaching, ...)."
        ),
    )

    @classmethod
    def describe(cls, step) -> dict:
        return {
            "uuid": step.uuid,
            "key": step.key,
            "name": step.name,
            "description": step.description,
            "kind": step.kind,
            "phase": step.phase,
            "order": step.order,
            "duration_in_days": step.duration_in_days,
            "team_part": provider_calls.team_part(step),
            "per_requested_offering": step.per_requested_offering,
            "delegable_within_team": provider_calls.delegable_within_team(step),
            "notification_triggers": provider_calls.team_notification_triggers(step),
        }


class CallApplicantVisibilityConfigSerializer(UserAttributeConfigBaseSerializer):
    class Meta(UserAttributeConfigBaseSerializer.Meta):
        model = models.CallApplicantVisibilityConfig


class CallFormField(serializers.SlugRelatedField):
    """A form (checklist) named by UUID, among those the requesting user may
    see: the forms the whole portal shares and those of the user's own
    organisations. Whether the form fits the call it is attached to is
    checked by the serializer, which knows the call."""

    def get_queryset(self):
        queryset = super().get_queryset()
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return queryset.none()
        return proposal_permissions.forms_visible_to(user, queryset)


def refuse_form_of_another_organisation(checklist, manager, field):
    """Refuse attaching ``checklist`` to a call run by the call managing
    organisation ``manager`` when the form is another organisation's own."""
    if manager is not None and not proposal_permissions.form_fits_organization(
        checklist, manager.customer_id
    ):
        raise serializers.ValidationError(
            {field: _("This form belongs to another organization.")}
        )


class CallReviewerEligibilitySerializer(serializers.ModelSerializer):
    """Who may review for the call, kept apart from who may apply to it. Empty
    lists admit everyone; the rules are read as the applicant restrictions
    are (any of the email patterns, affiliations and identity sources, then
    every configured AAI requirement)."""

    user_email_patterns = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_nationalities = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_organization_types = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_assurance_levels = serializers.ListField(
        child=serializers.CharField(), required=False
    )

    class Meta:
        model = models.CallReviewerEligibility
        fields = [
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
            "user_nationalities",
            "user_organization_types",
            "user_assurance_levels",
        ]


class ProtectedCallSerializer(PublicCallSerializer):
    reference_code = serializers.CharField(source="backend_id", required=False)
    image = serializers.ImageField(
        required=False,
        allow_null=True,
        validators=[ImageValidator],
        help_text=(
            "Logo shown on the call's card in place of its initials. A PNG, "
            "JPEG or GIF of at most 2 MB; null or an empty value removes it."
        ),
    )
    fixed_duration_in_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    allowed_duration_months = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=1200),
        required=False,
        help_text=(
            "Durations, in months, the applicant chooses the project's length "
            "from. Empty offers no choice. Cannot be combined with "
            "fixed_duration_in_days. Proposals already submitted keep the "
            "length they asked for."
        ),
    )
    reviewer_identity_visible_to_submitters = serializers.BooleanField(
        help_text="Whether proposal applicants can see reviewer identities",
        required=False,
    )
    reviews_visible_to_submitters = serializers.BooleanField(
        help_text="Whether proposal applicants can see review comments and scores. "
        "Applicants still see what an applicant step lists as its input steps, "
        "such as the reviews they respond to.",
        required=False,
    )
    applicant_cc_roles = serializers.ListField(
        child=serializers.CharField(max_length=150),
        required=False,
        max_length=10,
        help_text=(
            "Names of the proposal roles whose holders are copied (Cc) on every "
            "mail to the applicant about a proposal: PROPOSAL.MANAGER for the "
            "principal investigator, PROPOSAL.ADMIN for the proposal "
            "administrators. Only the proposal's current team is copied. Empty "
            "copies nobody."
        ),
    )
    compliance_checklist = CallFormField(
        slug_field="uuid",
        queryset=checklist_models.Checklist.objects.filter(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        ),
        required=False,
        allow_null=True,
        help_text="Compliance checklist that proposals must complete before submission",
    )
    panel_chair = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=core_models.User.objects.all(),
        required=False,
        allow_null=True,
    )
    panel_chair_uuid = serializers.UUIDField(
        source="panel_chair.uuid", read_only=True, format="hex"
    )
    panel_chair_name = serializers.ReadOnlyField(source="panel_chair.full_name")
    panel_vice_chair = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=core_models.User.objects.all(),
        required=False,
        allow_null=True,
    )
    panel_vice_chair_uuid = serializers.UUIDField(
        source="panel_vice_chair.uuid", read_only=True, format="hex"
    )
    panel_vice_chair_name = serializers.ReadOnlyField(
        source="panel_vice_chair.full_name"
    )
    order_author = serializers.ChoiceField(
        choices=OrderAuthors.CHOICES,
        required=False,
        help_text="Whose name the orders for resources granted by this call carry.",
    )
    evaluation_start = serializers.ChoiceField(
        choices=EvaluationStart.CHOICES,
        required=False,
        help_text=(
            "When a submitted proposal's evaluation starts: on submission, or "
            "for all proposals of a round together at its cut-off. Cannot be "
            "changed while the call has proposals submitted or in review."
        ),
    )
    publish_results = serializers.ChoiceField(
        choices=ResultsPublication.CHOICES,
        required=False,
        help_text=(
            "When applicants learn the allocation decision: immediately, or "
            "for every proposal of a round together when a call manager "
            "publishes the round's results. Cannot be changed while decisions "
            "are held."
        ),
    )
    # The queryset is narrowed to the call's own people in get_fields(); what
    # stands here is only the schema's view of the field.
    order_author_user = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=core_models.User.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "The person orders are attributed to when the author is a named "
            "contact. Must hold a role on this call or on the organization "
            "managing it."
        ),
    )
    # allow_null is load-bearing on a dotted source over a nullable FK: without
    # it DRF raises SkipField and drops the key from the payload entirely, while
    # make_readonly_fields_required still marks it required in the generated
    # SDK. Most calls have no named contact, so that mismatch would be the norm.
    order_author_user_uuid = serializers.UUIDField(
        source="order_author_user.uuid",
        read_only=True,
        format="hex",
        allow_null=True,
    )
    order_author_user_name = serializers.CharField(
        source="order_author_user.full_name",
        read_only=True,
        allow_null=True,
    )
    compliance_checklist_name = serializers.CharField(
        source="compliance_checklist.name", read_only=True
    )
    project_name_template = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=255,
        help_text=PROJECT_NAME_TEMPLATE_HELP,
    )
    proposal_slug_template = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=(
            "Template for proposal slugs. Supports: {call_slug}, {round_slug}, "
            "{org_slug}, {year}, {month}, {counter}, {counter_padded}. "
            "Default: {round_slug}-{counter_padded}"
        ),
    )

    # Eligibility restriction fields (from UserDetailsMatchMixin)
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of email regex patterns. User must match one.",
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed affiliations. User must have one.",
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed identity sources (identity providers).",
    )
    user_nationalities = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed nationality codes (ISO 3166-1 alpha-2). User must have one.",
    )
    user_organization_types = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of allowed organization type URNs (SCHAC). User must match one.",
    )
    user_assurance_levels = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of required assurance URIs (REFEDS). User must have ALL of these.",
    )

    applicant_visibility_config = CallApplicantVisibilityConfigSerializer(
        required=False,
        allow_null=True,
    )
    reviewer_eligibility = CallReviewerEligibilitySerializer(
        required=False,
        allow_null=True,
        help_text=(
            "Who may review for the call: checked when a reviewer is invited, "
            "accepts, is suggested or is given the call's reviewer role. Kept "
            "apart from the restrictions on applicants."
        ),
    )

    proposal_field_config = CallProposalFieldConfigSerializer(required=False)
    proposal_field_metadata = serializers.SerializerMethodField(
        help_text="Per-field state, permitted transitions and downstream "
        "consumers for the Project details step."
    )

    @extend_schema_field(ProposalFieldMetadataSerializer(many=True))
    def get_proposal_field_metadata(self, obj) -> list[dict]:
        return get_proposal_field_metadata(obj)

    has_proposals = serializers.SerializerMethodField(
        help_text="Whether any proposal has been submitted to this call. "
        "Used by the frontend to gate slug-template and checklist fields."
    )

    target_decision_days = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        max_value=3650,
        help_text=(
            "Days from a round's cut-off to its decision the workflow is "
            "planned for, when the round has no allocation date. The workflow "
            "check compares the steps' durations with it."
        ),
    )
    template_uuid = serializers.UUIDField(
        source="template.uuid", read_only=True, format="hex", allow_null=True
    )
    template_name = serializers.CharField(
        source="template.name", read_only=True, allow_null=True
    )
    template_latest_version = serializers.IntegerField(
        source="template.version",
        read_only=True,
        allow_null=True,
        help_text="The template's current version.",
    )

    @extend_schema_field(serializers.BooleanField())
    def get_has_proposals(self, obj) -> bool:
        return models.Proposal.objects.filter(round__call=obj).exists()

    class Meta(PublicCallSerializer.Meta):
        fields = PublicCallSerializer.Meta.fields + (
            "created_by",
            "reference_code",
            "compliance_checklist",
            "compliance_checklist_name",
            "panel_chair",
            "panel_chair_uuid",
            "panel_chair_name",
            "panel_vice_chair",
            "panel_vice_chair_uuid",
            "panel_vice_chair_name",
            "proposal_slug_template",
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
            "user_nationalities",
            "user_organization_types",
            "user_assurance_levels",
            "applicant_visibility_config",
            "reviewer_eligibility",
            "proposal_field_config",
            "proposal_field_metadata",
            "has_proposals",
            "order_author",
            "order_author_user",
            "order_author_user_uuid",
            "order_author_user_name",
            "carry_over_drafts",
            "coi_declaration_text",
            "undecided_at_round_completion",
            "adoption_required",
            "budget_enforcement",
            "applicant_cc_roles",
            "target_decision_days",
            "template_uuid",
            "template_name",
            "template_version",
            "template_latest_version",
        )
        view_name = "proposal-protected-call-detail"
        protected_fields = ("manager",)

    def get_fields(self):
        fields = super().get_fields()
        contact = fields.get("order_author_user")
        if contact is not None:
            contact.queryset = self._order_author_candidates()
            # Same message whichever way the lookup failed, so the field cannot
            # be used to tell an unrelated account apart from one that does not
            # exist. It still says what a usable answer looks like.
            contact.error_messages["does_not_exist"] = _(
                "No such user, or they hold no role on this call or on the "
                "organization managing it."
            )
        return fields

    def _order_author_candidates(self):
        """Users this call may name as the author of its orders.

        Whoever is named starts receiving the call's order mail -- project
        name, order description, limits -- and, for offerings fulfilled by a
        helpdesk ticket, gets an account created for them on the helpdesk.
        Anyone holding UPDATE_CALL could otherwise point that at an arbitrary
        account in the deployment, so the choice is kept to people already
        attached to the call: its own team, the managing organisation, and
        that organisation's customer.

        Empty when there is no call to read roles from -- during creation, and
        while drf-spectacular is building the schema.
        """
        call = self.instance
        if not isinstance(call, models.Call):
            return core_models.User.objects.none()
        manager = call.manager
        return (
            permissions_utils.get_users(call)
            | permissions_utils.get_users(manager)
            | permissions_utils.get_users(manager.customer)
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        self._validate_order_author(attrs)
        self._validate_chairs(attrs)
        self._validate_duration_policy(attrs)
        if attrs.get("compliance_checklist") is not None:
            refuse_form_of_another_organisation(
                attrs["compliance_checklist"],
                attrs.get("manager") or getattr(self.instance, "manager", None),
                "compliance_checklist",
            )
        return attrs

    def validate_applicant_cc_roles(self, value):
        return models.normalize_applicant_cc_roles(value)

    def validate_image(self, image):
        if image and image.size > CALL_IMAGE_MAX_BYTES:
            raise serializers.ValidationError(
                _("The logo must not be larger than 2 MB.")
            )
        return image

    def _validate_duration_policy(self, attrs):
        """A call fixes the length of its projects or offers a list, not both.

        The settings page may change one of the two per request, so the one
        left out is read from the call.
        """
        if "allowed_duration_months" in attrs:
            try:
                attrs["allowed_duration_months"] = (
                    models.normalize_allowed_duration_months(
                        attrs["allowed_duration_months"]
                    )
                )
            except serializers.ValidationError as e:
                raise serializers.ValidationError({"allowed_duration_months": e.detail})
        call = self.instance if isinstance(self.instance, models.Call) else None
        allowed = attrs.get(
            "allowed_duration_months", call.allowed_duration_months if call else []
        )
        fixed = attrs.get(
            "fixed_duration_in_days", call.fixed_duration_in_days if call else None
        )
        if allowed and fixed:
            raise serializers.ValidationError(
                {
                    "allowed_duration_months"
                    if "allowed_duration_months" in attrs
                    else "fixed_duration_in_days": models.DURATION_POLICIES_EXCLUSIVE_MESSAGE
                }
            )

    def validate_evaluation_start(self, value):
        """Refuse a switch while proposals are being evaluated.

        A switch mid-round would leave the round split between proposals that
        started on submission and proposals that wait for the cut-off.
        """
        call = self.instance
        if (
            isinstance(call, models.Call)
            and value != call.evaluation_start
            and call.has_proposals_under_evaluation()
        ):
            raise serializers.ValidationError(models.EVALUATION_START_LOCKED_MESSAGE)
        if isinstance(call, models.Call) and call.has_open_ended_round():
            conflict = call.open_ended_round_conflict(evaluation_start=value)
            if conflict:
                raise serializers.ValidationError(conflict)
        if (
            isinstance(call, models.Call)
            and value != EvaluationStart.AT_CUTOFF
            and call.workflow_steps.filter(round_scoped=True).exists()
        ):
            raise serializers.ValidationError(models.ROUND_STEPS_NEED_CUTOFF_MESSAGE)
        return value

    def validate_publish_results(self, value):
        """Refuse a switch while decisions wait for publication.

        Held decisions are released by publishing their round; switching to
        ``immediately`` would not release them, it would only change what
        the next decision does.
        """
        call = self.instance
        if (
            isinstance(call, models.Call)
            and value != call.publish_results
            and call.has_held_decisions()
        ):
            raise serializers.ValidationError(models.PUBLISH_RESULTS_LOCKED_MESSAGE)
        if isinstance(call, models.Call) and call.has_open_ended_round():
            conflict = call.open_ended_round_conflict(publish_results=value)
            if conflict:
                raise serializers.ValidationError(conflict)
        return value

    def _validate_order_author(self, attrs):
        """A named contact the call ends up using has to be reachable.

        A missing contact is deliberately *not* an error. The settings page
        edits one field per request, so demanding the contact in the same
        request that selects "specific_user" would leave the manager unable to
        select it at all -- and allocation already falls back to the applicant
        rather than refusing to place the order.

        The check is on the state the call would be left in, not on the field
        that happens to be in this payload: one request names a contact who
        still has an address, an SSO sync later clears it, and a third request
        switches the mode to "specific_user" on its own. Validating only the
        incoming field would wave that last one through.

        Requests touching neither field are left alone, so a call already in
        that state can still have everything else edited.
        """
        missing = object()
        choice = attrs.get("order_author", missing)
        contact = attrs.get("order_author_user", missing)
        if choice is missing and contact is missing:
            return

        if choice is missing:
            choice = getattr(self.instance, "order_author", None)
        if contact is missing:
            contact = getattr(self.instance, "order_author_user", None)

        if (
            choice == OrderAuthors.SPECIFIC_USER
            and contact is not None
            and not contact.email
        ):
            raise serializers.ValidationError(
                {
                    "order_author_user": _(
                        "The named contact has no email address, so they "
                        "cannot receive mail about the orders placed in "
                        "their name."
                    )
                }
            )

    def _validate_panel_seat(self, user, label):
        if self.instance is None:
            if user is None:
                return None
            raise serializers.ValidationError(
                _(
                    "Assign panel members first; the %(seat)s is set on an "
                    "existing call."
                )
                % {"seat": label}
            )
        # The viewset gates every write on UPDATE_CALL held on the call or its
        # managing organisation, so no field-level check is needed here.
        if user is None:
            return None
        if (
            not permissions_utils.get_users(
                self.instance, permissions_enums.RoleEnum.CALL_PANEL_MEMBER
            )
            .filter(pk=user.pk)
            .exists()
        ):
            raise serializers.ValidationError(
                _("Panel %(seat)s must hold the panel member role on this call.")
                % {"seat": label}
            )
        return user

    def validate_panel_chair(self, user):
        return self._validate_panel_seat(user, "chair")

    def validate_panel_vice_chair(self, user):
        return self._validate_panel_seat(user, "vice-chair")

    def _validate_chairs(self, attrs):
        """The chair and the vice-chair are two people."""

        def seat(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        chair, vice_chair = seat("panel_chair"), seat("panel_vice_chair")
        if chair is not None and vice_chair is not None and chair.pk == vice_chair.pk:
            raise serializers.ValidationError(
                {
                    "panel_vice_chair": _(
                        "The vice-chair cannot be the panel chair as well."
                    )
                }
            )

    def validate_manager(self, manager: models.CallManagingOrganisation):
        user = self.context["request"].user

        # The user's roles, not the request, deliberately: manager is set on
        # create only, and create() checks CALL.CREATE on it with the request,
        # applying a token's scopes and bindings. Checking the request here too
        # would also require CALL.CREATE_PERMISSION among a token's scopes.
        if (
            manager
            and not user.is_staff
            and not permissions_utils.has_permission(
                user, permissions_enums.PermissionEnum.CREATE_CALL_PERMISSION, manager
            )
        ):
            raise serializers.ValidationError(
                "Current user does not belong to the selected organisation."
            )

        return manager

    def validate_proposal_field_config(self, value):
        """Refuse to make a field required once the call has proposals.

        Only that direction is refused. Hiding a field, or dropping it back to
        optional, cannot invalidate a draft that was already complete; demanding
        something the applicant was never asked for can, and silently — nothing
        notifies them that the form they filled in has changed under them. A
        manager who wants a stricter next round duplicates the call, where the
        copy starts in draft with no proposals.
        """
        call: models.Call = self.instance
        if call is None or not value:
            return value
        if not models.Proposal.objects.filter(round__call=call).exists():
            return value

        current = models.CallProposalFieldConfig.get_states_for_call(call)
        tightened = [
            field_name
            for field_name in models.CallProposalFieldConfig.field_names()
            if value.get(models.CallProposalFieldConfig.column_for(field_name))
            == ProposalFieldStates.REQUIRED
            and current[field_name] != ProposalFieldStates.REQUIRED
        ]
        if tightened:
            raise serializers.ValidationError(
                "Cannot make %(fields)s required: this call already has proposals, "
                "and drafts that were complete under the published form would stop "
                "being submittable. Duplicate the call to run a stricter round."
                % {"fields": ", ".join(sorted(tightened))}
            )
        return value

    def validate_compliance_checklist(self, value):
        """Prevent changing compliance checklist if proposals exist."""
        call: models.Call = self.instance
        if call and models.Proposal.objects.filter(round__call=call).exists():
            if value != call.compliance_checklist:
                raise serializers.ValidationError(
                    "Cannot change compliance checklist when proposals exist"
                )
        return value

    def validate_project_name_template(self, value):
        """Only the documented placeholders, and a name the deployment's
        project name rule accepts. Empty means the default name. Not locked
        once proposals exist: names are given at allocation, so a change
        applies to the proposals allocated after it."""
        if not value or not value.strip():
            return None
        errors = allocation_names.project_name_template_errors(value)
        if errors:
            raise serializers.ValidationError(errors)
        # The literal text of the template ends up in every project name, so
        # it has to pass the rule project names are held to.
        sample = value.format_map(
            {name: "Example" for name in PROJECT_NAME_PLACEHOLDERS}
        )
        error = get_project_name_regex_error(sample)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_proposal_slug_template(self, value):
        """Validate that the template only uses allowed placeholders."""
        # Prevent changing template if proposals exist. An empty template
        # and no template both mean the default one.
        call: models.Call = self.instance
        if (
            call
            and (value or None) != (call.proposal_slug_template or None)
            and call.has_proposals()
        ):
            raise serializers.ValidationError(
                "Cannot change proposal slug template when proposals exist"
            )

        if not value:
            return value

        # Extract all placeholders from the template (handle format specs like {counter:03d})
        placeholders = re.findall(r"\{([^}:]+)", value)

        allowed_placeholders = {
            "call_slug",
            "round_slug",
            "org_slug",
            "year",
            "month",
            "counter",
            "counter_padded",
        }

        invalid_placeholders = set(placeholders) - allowed_placeholders

        if invalid_placeholders:
            raise serializers.ValidationError(
                f"Invalid placeholders: {', '.join(sorted(invalid_placeholders))}. "
                f"Allowed: {', '.join(sorted(allowed_placeholders))}"
            )

        # Validate the template by attempting a test format
        test_context = {
            "call_slug": "TEST-CALL",
            "round_slug": "TEST-ROUND-202401",
            "org_slug": "TEST-ORG",
            "year": "2024",
            "month": "01",
            "counter": "1",
            "counter_padded": "001",
        }

        try:
            value.format(**test_context)
        except (KeyError, ValueError) as e:
            raise serializers.ValidationError(f"Invalid template format: {e}")

        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return data
        if data.get("applicant_visibility_config") is None:
            synthetic = models.CallApplicantVisibilityConfig(call=instance)
            data["applicant_visibility_config"] = (
                CallApplicantVisibilityConfigSerializer(
                    synthetic, context=self.context
                ).data
            )
        if data.get("reviewer_eligibility") is None:
            data["reviewer_eligibility"] = CallReviewerEligibilitySerializer(
                models.CallReviewerEligibility(call=instance)
            ).data
        return data

    @staticmethod
    def _apply_reviewer_eligibility(call, eligibility_data):
        """Replace the call's reviewer rules (None clears them); a PATCH
        carries only the lists it changes."""
        if eligibility_data is None:
            models.CallReviewerEligibility.objects.filter(call=call).delete()
            return
        eligibility, _ = models.CallReviewerEligibility.objects.get_or_create(call=call)
        for key, value in eligibility_data.items():
            setattr(eligibility, key, value)
        eligibility.save()

    def create(self, validated_data):
        request = self.context["request"]
        customer = validated_data.get("manager", None).customer
        if not permissions_utils.has_permission(
            request,
            permissions_enums.PermissionEnum.CREATE_CALL,
            customer.callmanagingorganisation,
        ):
            raise PermissionDenied()

        validated_data["created_by"] = request.user
        has_visibility = "applicant_visibility_config" in validated_data
        visibility_data = validated_data.pop("applicant_visibility_config", None)
        field_config_data = validated_data.pop("proposal_field_config", None)
        has_eligibility = "reviewer_eligibility" in validated_data
        eligibility_data = validated_data.pop("reviewer_eligibility", None)
        call = super().create(validated_data)
        if has_eligibility and eligibility_data is not None:
            self._apply_reviewer_eligibility(call, eligibility_data)
        # The row itself is seeded by the post_save handler from the Constance
        # defaults; an explicit config on the request overrides those columns.
        if field_config_data:
            self._apply_field_config(call, field_config_data)
        if has_visibility and visibility_data is not None:
            seed = models.CallApplicantVisibilityConfig.get_default_exposure_flags()
            models.CallApplicantVisibilityConfig.objects.create(
                call=call, **{**seed, **visibility_data}
            )
        return call

    @transaction.atomic
    def update(self, instance, validated_data):
        # A changed fixed_duration_in_days needs no propagation: allocation
        # reads it from the call (utils.project_end_date), so it takes effect
        # for every proposal that has not been allocated yet without rewriting
        # a single proposal row.
        if (
            validated_data.get("resource_template_selection_mode")
            == ResourceTemplateSelectionModes.SINGLE
            and instance.resource_template_selection_mode
            != ResourceTemplateSelectionModes.SINGLE
        ):
            # Same lock RequestedResourceSerializer.create takes: a request
            # added concurrently either shows up here or sees single mode.
            models.Call.objects.select_for_update(no_key=True).get(pk=instance.pk)
            instance.check_can_allow_one_request_only()
        has_visibility = "applicant_visibility_config" in validated_data
        visibility_data = validated_data.pop("applicant_visibility_config", None)
        field_config_data = validated_data.pop("proposal_field_config", None)
        has_eligibility = "reviewer_eligibility" in validated_data
        eligibility_data = validated_data.pop("reviewer_eligibility", None)
        call = super().update(instance, validated_data)
        if has_eligibility:
            self._apply_reviewer_eligibility(call, eligibility_data)
        if field_config_data:
            self._apply_field_config(call, field_config_data)
        if has_visibility:
            if visibility_data is None:
                models.CallApplicantVisibilityConfig.objects.filter(call=call).delete()
            elif (
                existing := models.CallApplicantVisibilityConfig.objects.filter(
                    call=call
                ).first()
            ) is not None:
                # Row exists — partial PATCH only touches supplied keys.
                for key, value in visibility_data.items():
                    setattr(existing, key, value)
                existing.save()
            else:
                # First create — seed Constance defaults so unspecified fields
                # don't silently fall back to model defaults.
                seed = models.CallApplicantVisibilityConfig.get_default_exposure_flags()
                models.CallApplicantVisibilityConfig.objects.create(
                    call=call, **{**seed, **visibility_data}
                )
        return call

    @staticmethod
    def _apply_field_config(call, field_config_data: dict):
        """Write the supplied field states onto the call's config row.

        A PATCH carries only the fields it changes, so the row is updated in
        place rather than replaced. get_or_create covers calls that predate the
        seeding handler.
        """
        config, _ = models.CallProposalFieldConfig.objects.get_or_create(call=call)
        for column, state in field_config_data.items():
            setattr(config, column, state)
        config.save()


class RoundStepSerializer(serializers.Serializer):
    """A step of the call in one round: its due date there, and for a step
    run once per round its opening and round reports."""

    step_uuid = serializers.UUIDField(source="call_step.uuid", format="hex")
    step_key = serializers.CharField(source="call_step.key")
    step_name = serializers.CharField(source="call_step.name")
    step_kind = serializers.CharField(source="call_step.kind")
    order = serializers.IntegerField(source="call_step.order")
    round_scoped = serializers.BooleanField(source="call_step.round_scoped")
    has_reports = serializers.BooleanField()
    due_at = serializers.DateTimeField(allow_null=True)
    opened_at = serializers.DateTimeField(allow_null=True)
    proposals_at_step = serializers.IntegerField(
        help_text="Proposals of the round standing at the step now."
    )
    proposals_before_step = serializers.IntegerField(
        help_text="Proposals of the round still to reach the step."
    )
    proposals_awaiting_advance = serializers.IntegerField(
        help_text=(
            "Proposals parked on the step once completed, waiting to be moved on."
        )
    )
    reports_total = serializers.IntegerField()
    reports_submitted = serializers.IntegerField()


class ProtectedRoundSerializer(
    core_serializers.AugmentedSerializerMixin, NestedRoundSerializer
):
    url = serializers.SerializerMethodField()
    proposals = serializers.SerializerMethodField()
    review_duration_in_days = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text=(
            "Deprecated: a review is due when the step it contributes to is "
            "(see the round's step due dates). Only a review whose step has no "
            "deadline is still due this many days after it was created."
        ),
    )
    results_published_by_name = serializers.CharField(
        source="results_published_by.full_name", read_only=True, allow_null=True
    )
    held_decisions_count = serializers.SerializerMethodField(
        help_text=(
            "Proposals of the round whose decision is recorded but not yet "
            "published. After publication, decisions that could not be "
            "carried out; publishing the results again retries them. Null "
            "for anyone who may not see held decisions."
        )
    )
    round_steps = serializers.SerializerMethodField(
        help_text=(
            "The call's enabled steps in this round: the due date of each "
            "there, how many proposals stand before, at and parked on it, and "
            "for a step run once per round its opening and round reports."
        )
    )
    has_proposals = serializers.SerializerMethodField(
        help_text=(
            "Whether the round holds any proposal, in any state. A round with "
            "proposals cannot be deleted. Unlike the proposals list, which "
            "leaves out what the viewer may not see, this counts every one."
        )
    )

    class Meta(NestedRoundSerializer.Meta):
        fields = NestedRoundSerializer.Meta.fields + [
            "url",
            "proposals",
            "results_published_by_name",
            "held_decisions_count",
            "has_proposals",
            "round_steps",
            "results_forced_reason",
            "adoption_note",
            "adoption_document",
            "undecided_at_round_completion",
        ]
        # The adoption is recorded through the round's record_adoption action.
        read_only_fields = NestedRoundSerializer.Meta.read_only_fields + [
            "results_forced_reason",
            "adoption_note",
            "adoption_document",
        ]
        # Null for whoever may not read the adoption yet; see to_representation.
        extra_kwargs = {
            **getattr(NestedRoundSerializer.Meta, "extra_kwargs", {}),
            **{
                field: {"allow_null": True}
                for field in (
                    "results_forced_reason",
                    "adoption_note",
                    "adoption_document",
                )
            },
        }

    ADOPTION_FIELDS = ("results_forced_reason", "adoption_note", "adoption_document")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self._can_view_adoption(instance):
            for field in self.ADOPTION_FIELDS:
                if field in data:
                    data[field] = None
        return data

    def _can_view_adoption(self, call_round) -> bool:
        """Whether the viewer may read the round's adoption record yet.

        Until the results are published the record states the outcome, which
        only those who may see held decisions know; evaluators on the call
        learn it with everyone else. Checked once per call.
        """
        if not call_round.withholds_results:
            return True
        request = self.context.get("request")
        if not request:
            return False
        cache = self.context.setdefault("_held_decisions_visible", {})
        if call_round.call_id not in cache:
            cache[call_round.call_id] = (
                proposal_permissions.user_can_view_held_decisions_of_call(
                    request.user, call_round.call
                )
            )
        return cache[call_round.call_id]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")

        # Only allow staff to edit slug field
        if request and not (request.user and request.user.is_staff):
            if "slug" in fields:
                fields["slug"].read_only = True

        return fields

    @extend_schema_field(RoundStepSerializer(many=True))
    def get_round_steps(self, call_round):
        # Counted once per call for every round being listed.
        cache = self.context.setdefault("_round_steps", {})
        if call_round.pk not in cache:
            listed = getattr(getattr(self, "parent", None), "instance", None)
            rounds = (
                list(listed)
                if listed is not None and not isinstance(listed, models.Round)
                else [call_round]
            )
            round_ids = [
                r.pk
                for r in rounds
                if isinstance(r, models.Round) and r.call_id == call_round.call_id
            ]
            if call_round.pk not in round_ids:
                round_ids.append(call_round.pk)
            cache.update(round_reports.round_steps_by_round(call_round.call, round_ids))
        return RoundStepSerializer(cache[call_round.pk], many=True).data

    def get_has_proposals(self, call_round) -> bool:
        # Round lists annotate the count; a single round asks here.
        count = getattr(call_round, "proposals_count", None)
        if count is None:
            return call_round.proposal_set.exists()
        return count > 0

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_held_decisions_count(self, call_round) -> int | None:
        # Null for anyone but the call managers (and staff and support): to
        # everyone else a held decision is still being made, see
        # ProposalSerializer.get_decision_held. Checked once per call.
        request = self.context.get("request")
        if not request:
            return None
        cache = self.context.setdefault("_held_decisions_visible", {})
        if call_round.call_id not in cache:
            cache[call_round.call_id] = (
                proposal_permissions.user_can_view_held_decisions_of_call(
                    request.user, call_round.call
                )
            )
        if not cache[call_round.call_id]:
            return None
        # Round lists annotate the count; a single round counts it here.
        count = getattr(call_round, "held_decisions_count", None)
        if count is None:
            count = call_round.proposal_set.filter(decision_held=True).count()
        # Not counting the decisions held from the reader: their own, when
        # they also apply to the call they manage.
        if count and not (request.user.is_staff or request.user.is_support):
            if "_team_proposal_ids" not in self.context:
                self.context["_team_proposal_ids"] = set(
                    proposal_roles.applicant_team_proposal_ids(request.user)
                )
            team = self.context["_team_proposal_ids"]
            if team:
                count -= call_round.proposal_set.filter(
                    decision_held=True, pk__in=team
                ).count()
        return count

    @extend_schema_field(ProtectedProposalListSerializer(many=True))
    def get_proposals(self, call_round):
        """The round's proposals as this viewer may see them.

        Every call role lists the round, evaluators included. An evaluator
        (see ``get_concealed_applicant_attributes``) is not shown other
        applicants' drafts -- an unsubmitted proposal is nothing to evaluate --
        and sees the creator only as far as the call's applicant visibility
        config exposes it.
        """
        request = self.context.get("request")
        user = getattr(request, "user", None)
        visibility = get_applicant_visibility(self.context, user)
        rows = []
        if "proposal_set" in getattr(call_round, "_prefetched_objects_cache", {}):
            # A round list prefetches them for every row at once.
            proposals = call_round.proposal_set.all()
        else:
            proposals = call_round.proposal_set.select_related(
                "round__call", "created_by", "approved_by"
            )
        for proposal in proposals:
            concealed = visibility.concealed_attributes(proposal)
            if concealed is not None and proposal.state == ProposalStates.DRAFT:
                continue
            data = ProtectedProposalListSerializer(proposal, context=self.context).data
            for attr in concealed or ():
                for field_name in APPLICANT_FIELD_MAP.get(attr, []):
                    data.pop(field_name, None)
            rows.append(data)
        return rows

    def get_url(self, call_round) -> str:
        return self.context["request"].build_absolute_uri(
            reverse(
                "proposal-call-round-detail",
                kwargs={
                    "uuid": call_round.call.uuid.hex,
                    "obj_uuid": call_round.uuid.hex,
                },
            )
        )

    def create(self, validated_data):
        # Set a default value if not provided by the user
        if "review_duration_in_days" not in validated_data:
            validated_data["review_duration_in_days"] = config.PROPOSAL_REVIEW_DURATION

        return super().create(validated_data)

    def validate(self, attrs):
        # What the round would be left with: a partial update keeps the rest.
        start_time = attrs.get("start_time", getattr(self.instance, "start_time", None))
        cutoff_time = attrs.get(
            "cutoff_time", getattr(self.instance, "cutoff_time", None)
        )

        if start_time and cutoff_time and cutoff_time <= start_time:
            raise serializers.ValidationError(
                {"start_time": _("Cutoff time must be later than start time.")}
            )

        call = self.context["view"].get_object()

        if cutoff_time is None:
            if self.instance is not None and self.instance.lifecycle_state:
                raise serializers.ValidationError(
                    {
                        "cutoff_time": _(
                            "A round past its cut-off cannot be reopened. Add a "
                            "new round instead."
                        )
                    }
                )
            conflict = call.open_ended_round_conflict()
            if conflict:
                raise serializers.ValidationError({"cutoff_time": conflict})

        others = models.Round.objects.filter(call=call).exclude(
            uuid=getattr(self.instance, "uuid", None)
        )
        if start_time and round_overlaps(others, start_time, cutoff_time).exists():
            raise serializers.ValidationError(
                "Round is overlapping with another round."
            )

        return attrs


class ProposalDocumentationSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="file.name", read_only=True)
    file_size = FileSizeField()

    class Meta:
        model = models.ProposalDocumentation
        fields = ["uuid", "file", "file_name", "file_size", "created"]
        read_only_fields = ["uuid"]
        # The model field is nullable, so it would be optional here; a document
        # without a file is meaningless and breaks reading the proposal.
        extra_kwargs = {
            "file": {
                "validators": [DocumentValidator],
                "required": True,
                "allow_null": False,
            }
        }

    def validate_file(self, upload):
        """Refuse a file the proposal already holds. Storage renames a second
        upload of the same name, so the stored hash is compared instead."""
        proposal = self.context.get("proposal")
        if proposal is None:
            return upload
        # Hashed in chunks from the start, whatever an earlier validator left
        # the position at, and rewound for the storage that saves it.
        upload.seek(0)
        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        digest = digest.hexdigest()
        upload.seek(0)
        stored = proposal.proposaldocumentation_set.exclude(file="").values_list(
            "file", flat=True
        )
        if media_models.File.objects.filter(name__in=stored, hash=digest).exists():
            raise serializers.ValidationError(
                _("This file is already attached to the proposal.")
            )
        return upload


class ProposalDetachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.UUIDField())


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n")


# The limit stays in the schema although the field carries no validator: the
# serializer's validate_project_summary applies it, minus the grandfather rule.
@extend_schema_field({"type": "string", "maxLength": models.PROJECT_SUMMARY_LENGTH})
class GrandfatheredProjectSummaryField(serializers.CharField):
    pass


class ProposalUpdateProjectDetailsSerializer(serializers.ModelSerializer):
    science_sub_domain = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.ScienceSubDomain.objects.all(),
        allow_null=True,
        required=False,
    )
    # As on ProposalSerializer: the form saves through this action, so what
    # create sanitises must be sanitised here too.
    description = core_serializers.HTMLCleanField(
        required=False, allow_blank=True, max_length=DESCRIPTION_LENGTH
    )
    # Declared without the model's max_length, which would run before
    # validate_project_summary: a summary stored over the limit before it
    # existed stays editable as long as it is sent back unchanged. Only a new
    # or edited value has to fit.
    project_summary = GrandfatheredProjectSummaryField(required=False, allow_blank=True)
    requested_duration_months = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text=(
            "The project length, in months: one of the call's allowed_duration_months."
        ),
    )

    class Meta:
        model = models.Proposal
        fields = [
            "name",
            "description",
            "project_summary",
            "science_sub_domain",
            "requested_duration_months",
        ]

    def validate_project_summary(self, value):
        # A textarea hands back LF where the row may hold CRLF (an import, the
        # API): that is not an edit.
        stored = (self.instance.project_summary or "") if self.instance else None
        unchanged = stored is not None and _normalize_newlines(
            value
        ) == _normalize_newlines(stored.strip())
        if len(value) > models.PROJECT_SUMMARY_LENGTH and not unchanged:
            raise serializers.ValidationError(
                self.fields["project_summary"]
                .error_messages["max_length"]
                .format(max_length=models.PROJECT_SUMMARY_LENGTH)
            )
        return value

    def validate_requested_duration_months(self, value):
        if value is None:
            return value
        proposal = self.instance
        allowed = proposal.round.call.allowed_duration_months
        if not allowed:
            raise serializers.ValidationError(
                _("This call offers no choice of duration.")
            )
        if value not in allowed:
            raise serializers.ValidationError(models.duration_options_message(allowed))
        too_long = prepaid_months_above(
            proposal.requestedresource_set.select_related(
                "requested_offering__offering"
            ),
            value,
        )
        if too_long:
            raise serializers.ValidationError(
                _(
                    "Subscriptions requested for %(offerings)s run longer than "
                    "%(months)s month(s); shorten them first."
                )
                % {"offerings": ", ".join(too_long), "months": value}
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        self._refuse_locked_changes(attrs)
        return attrs

    def _refuse_locked_changes(self, attrs):
        """Once submitted, a field changes only while an open request for
        changes unlocks it. A field sent with the value it has is no change."""
        proposal = self.instance
        if proposal.state == ProposalStates.DRAFT:
            return
        unlocked = change_requests.unlocked_parts(proposal)
        locked = [
            name
            for name, value in attrs.items()
            if value != getattr(proposal, name)
            and ChangeRequestParts.field(name) not in unlocked
        ]
        if locked:
            # A conflict with the proposal's state, as every locked part is.
            raise IncorrectStateException(
                {name: change_requests.LOCKED_MESSAGE for name in locked}
            )


class ProposalRunSerializer(serializers.ModelSerializer):
    """An extension request or final report of an accepted proposal."""

    opened_at = serializers.DateTimeField(source="created", read_only=True)
    opened_by_name = serializers.ReadOnlyField(
        source="opened_by.full_name", allow_null=True
    )
    granted_by_name = serializers.ReadOnlyField(
        source="granted_by.full_name",
        allow_null=True,
        help_text="Extension: who set what the decision grants.",
    )
    active_step_uuid = serializers.SerializerMethodField(
        help_text="The run's step that is running now, if any."
    )
    active_step_kind = serializers.SerializerMethodField()

    class Meta:
        model = models.ProposalRun
        fields = (
            "uuid",
            "phase",
            "state",
            "opened_at",
            "opened_by_name",
            "closed_at",
            "requested_end_date",
            "requested_limits",
            "granted_end_date",
            "granted_limits",
            "granted_by_name",
            "applied_changes",
            "active_step_uuid",
            "active_step_kind",
        )
        read_only_fields = fields

    def _active_step(self, obj):
        if not hasattr(obj, "_active_step_cache"):
            obj._active_step_cache = (
                obj.step_instances.filter(status=WorkflowStepInstanceStatuses.ACTIVE)
                .select_related("call_step")
                .order_by("order", "id")
                .first()
            )
        return obj._active_step_cache

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_active_step_uuid(self, obj):
        step = self._active_step(obj)
        return step.uuid.hex if step else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_active_step_kind(self, obj):
        step = self._active_step(obj)
        return step.call_step.kind if step else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = request.user if request else None
        # The applicant team reads what was granted once it is granted: a
        # draft is never theirs, and neither is one a rejected or withdrawn
        # extension leaves behind.
        if (
            user is not None
            and instance.state != ProposalRunStates.APPROVED
            and not user.is_staff
            and proposal_roles.is_on_applicant_team(user, instance.proposal)
            and not proposal_permissions.user_drives_runs(user, instance.proposal)
        ):
            data["granted_end_date"] = None
            data["granted_limits"] = {}
            data["granted_by_name"] = None
        if user is not None:
            scope = contributions.access_for(request).run_scope(instance.proposal)
            if scope is not None:
                self._narrow_to_offerings(data, scope)
        return data

    @staticmethod
    def _narrow_to_offerings(data, offering_ids):
        """Keep only what concerns the resources of ``offering_ids``: a
        provider reads their own offerings' limits and changes, not the
        others'. The project's end date is the whole award's."""
        applied = data.get("applied_changes") or {}
        keys = (
            set(data.get("requested_limits") or {})
            | set(data.get("granted_limits") or {})
            | {
                row.get("resource_uuid")
                for name in ("resource_end_dates", "orders", "skipped")
                for row in applied.get(name) or []
            }
        ) - {None}
        own = set()
        if keys:
            valid = []
            for key in keys:
                try:
                    valid.append(uuid.UUID(str(key)).hex)
                except ValueError:
                    continue
            own = {
                uuid.UUID(str(value)).hex
                for value in marketplace_models.Resource.objects.filter(
                    uuid__in=valid, offering_id__in=offering_ids
                ).values_list("uuid", flat=True)
            }
        for field in ("requested_limits", "granted_limits"):
            if data.get(field):
                data[field] = {
                    key: value for key, value in data[field].items() if key in own
                }
        if applied:
            data["applied_changes"] = {
                name: (
                    [row for row in value if row.get("resource_uuid") in own]
                    if isinstance(value, list)
                    else value
                )
                for name, value in applied.items()
            }


class ProposalRunUpdateSerializer(serializers.Serializer):
    """What an extension asks for, while its request is open, and what it
    grants, while its decision is."""

    requested_end_date = serializers.DateField(required=False, allow_null=True)
    requested_limits = serializers.JSONField(required=False)
    granted_end_date = serializers.DateField(required=False, allow_null=True)
    granted_limits = serializers.JSONField(required=False)

    def validate(self, attrs):
        proposal = self.context["proposal"]
        for field in ("requested_end_date", "granted_end_date"):
            if attrs.get(field) is not None:
                try:
                    attrs[field] = runs.validate_end_date(proposal, attrs[field])
                except serializers.ValidationError as exc:
                    raise serializers.ValidationError({field: exc.detail})
        for field in ("requested_limits", "granted_limits"):
            if field in attrs:
                try:
                    attrs[field] = runs.validate_limits(proposal, attrs[field])
                except serializers.ValidationError as exc:
                    raise serializers.ValidationError({field: exc.detail})
        return attrs


class RequestExtensionSerializer(serializers.Serializer):
    requested_end_date = serializers.DateField(required=False, allow_null=True)
    requested_limits = serializers.JSONField(required=False)

    def validate(self, attrs):
        return ProposalRunUpdateSerializer.validate(self, attrs)


class WithdrawRunSerializer(serializers.Serializer):
    run_uuid = serializers.UUIDField(
        required=False,
        help_text="The run to call off; the proposal's open one when left out.",
    )


class ArchiveCallSerializer(serializers.Serializer):
    confirm = serializers.BooleanField(
        default=False,
        help_text="Archive even though proposals are still in evaluation.",
    )


class ProvisionAwardedItemSerializer(serializers.Serializer):
    awarded_resource_uuid = serializers.UUIDField(
        help_text="The awarded item to order again."
    )


class ProposalAwardedDurationSerializer(serializers.Serializer):
    awarded_duration_months = serializers.IntegerField(
        min_value=1,
        help_text=(
            "The project length the award grants, in months: one of the "
            "call's allowed_duration_months."
        ),
    )

    def validate_awarded_duration_months(self, value):
        proposal = self.context["proposal"]
        allowed = proposal.round.call.allowed_duration_months
        if not allowed:
            raise serializers.ValidationError(
                _("This call offers no choice of duration.")
            )
        if value not in allowed:
            raise serializers.ValidationError(models.duration_options_message(allowed))
        too_long = prepaid_months_above(
            proposal.awarded_resources.filter(
                requested_offering__state=RequestedOfferingStates.ACCEPTED
            ).select_related("requested_offering__offering"),
            value,
        )
        if too_long:
            raise serializers.ValidationError(
                _(
                    "Subscriptions awarded on %(offerings)s run longer than "
                    "%(months)s month(s); shorten them first."
                )
                % {"offerings": ", ".join(too_long), "months": value}
            )
        return value


NO_SUCH_PANEL = _("The call has no such panel.")


class PanelField(serializers.SlugRelatedField):
    """A panel by uuid; a missing one is refused as one of another call is."""

    default_error_messages = {
        **serializers.SlugRelatedField.default_error_messages,
        "does_not_exist": NO_SUCH_PANEL,
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("slug_field", "uuid")
        kwargs.setdefault("queryset", models.CallPanel.objects.all())
        super().__init__(**kwargs)


class ProposalSetPanelSerializer(serializers.Serializer):
    panel = PanelField(
        allow_null=True,
        help_text=(
            "The panel of the proposal's call that evaluates it from now on; "
            "null takes it off any panel."
        ),
    )


class ProposalComplianceStatusSerializer(serializers.Serializer):
    error = serializers.CharField(required=False)
    has_checklist = serializers.BooleanField()
    is_completed = serializers.BooleanField()
    requires_review = serializers.BooleanField()
    completion_percentage = serializers.IntegerField()
    reviewed_by = serializers.CharField(allow_null=True)
    reviewed_at = serializers.DateTimeField(allow_null=True)
    checklist_name = serializers.CharField(required=False)
    unanswered_required_count = serializers.IntegerField(required=False)


class ProposalWithdrawSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ProposalCanSubmitResponseSerializer(serializers.Serializer):
    can_submit = serializers.BooleanField()
    error = serializers.CharField(allow_null=True)


class ProposalChangeRequestSerializer(serializers.ModelSerializer):
    """A round of changes asked for on a proposal, or a reopened contribution."""

    step_uuid = serializers.UUIDField(
        source="step_instance.uuid",
        read_only=True,
        help_text="UUID of the workflow step instance the changes were asked at.",
    )
    step_key = serializers.CharField(source="step_instance.step", read_only=True)
    step_name = serializers.SerializerMethodField()
    contribution_uuid = serializers.UUIDField(
        source="contribution.uuid", read_only=True, allow_null=True
    )
    requested_by_name = serializers.SerializerMethodField()
    is_open = serializers.BooleanField(read_only=True)
    unlocked_parts = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text=(
            "The parts the applicant may edit while the request is open: "
            "field:<name>, documents, resources, team, compliance."
        ),
    )

    class Meta:
        model = models.ProposalChangeRequest
        fields = [
            "uuid",
            "created",
            "step_uuid",
            "step_key",
            "step_name",
            "contribution_uuid",
            "message",
            "unlocked_parts",
            "due_at",
            "is_open",
            "closed_at",
            "resolution",
            "requested_by_name",
        ]
        read_only_fields = fields

    def get_step_name(self, obj) -> str:
        return obj.step_instance.call_step.name or obj.step_instance.step

    def get_requested_by_name(self, obj) -> str | None:
        """Who asked; not told to the applicant team, as the step's actor may
        be one of its evaluators."""
        if obj.requested_by is None:
            return None
        hidden = self.context.get("conceals_requester")
        if hidden is not None and hidden(obj):
            return None
        return obj.requested_by.full_name or obj.requested_by.username


class RequestChangesSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        help_text=(
            "UUID of the active workflow step instance the changes are asked at."
        ),
    )
    message = serializers.CharField(
        help_text="What the applicant is to change. The applicant reads it.",
    )
    parts = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
        help_text=(
            "The parts the applicant may edit until the changes are submitted: "
            "field:<name> for a Project details field, documents, resources, "
            "team or compliance; at a step of an extension request or final "
            "report, run_form (its applicant step's answers) and, for an "
            "extension, run_request (the end date and limits asked for). "
            "unlockable_parts on the proposal lists them."
        ),
    )
    due_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "When the changes are due. Past it, the request lapses and the step "
            "returns to its actor; nothing is decided for them."
        ),
    )


class CancelChangeRequestSerializer(serializers.Serializer):
    change_request_uuid = serializers.UUIDField()


class ReopenContributionSerializer(serializers.Serializer):
    contribution_uuid = serializers.UUIDField()
    message = serializers.CharField(
        help_text=(
            "What is to change, for the contributor. Never shown to the applicant."
        ),
    )


class UnlockablePartsSerializer(serializers.Serializer):
    parts = serializers.ListField(child=serializers.CharField())


class ProposalStatusLabelSerializer(serializers.Serializer):
    step = serializers.CharField(help_text="Key of the step the label is for.")
    label = serializers.CharField()


class OfferingTeamProviderSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    name = serializers.CharField()


class ProposalSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField()
    round = NestedRoundSerializer(read_only=True)
    round_uuid = serializers.UUIDField(write_only=True, required=True)
    call_uuid = serializers.UUIDField(source="round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="round.call.name", read_only=True)
    call_managing_organisation_uuid = serializers.ReadOnlyField(
        source="round.call.manager.uuid"
    )
    call_adoption_required = serializers.BooleanField(
        source="round.call.adoption_required",
        read_only=True,
        help_text=(
            "Whether the call's round results are adopted, after which a held "
            "decision can no longer be reopened."
        ),
    )
    supporting_documentation = ProposalDocumentationSerializer(
        many=True, read_only=True, source="proposaldocumentation_set"
    )
    oecd_fos_2007_label = serializers.CharField(
        read_only=True, source="get_oecd_fos_2007_code_display"
    )
    science_sub_domain = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.ScienceSubDomain.objects.all(),
        allow_null=True,
        required=False,
    )
    science_sub_domain_name = serializers.ReadOnlyField(
        source="science_sub_domain.name",
    )
    science_domain_uuid = serializers.ReadOnlyField(
        source="science_sub_domain.domain.uuid",
    )
    science_domain_name = serializers.ReadOnlyField(
        source="science_sub_domain.domain.name",
    )
    panel_uuid = serializers.UUIDField(
        source="panel.uuid",
        format="hex",
        read_only=True,
        allow_null=True,
        help_text=(
            "The call panel evaluating the proposal. Null for whoever may not "
            "see who evaluates it, the applicant team always."
        ),
    )
    panel_name = serializers.ReadOnlyField(
        source="panel.name",
        allow_null=True,
        help_text="The name of the call panel, as panel_uuid is shown.",
    )
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_uuid = serializers.UUIDField(source="created_by.uuid", read_only=True)
    project_name = serializers.ReadOnlyField(source="project.name")
    description = core_serializers.HTMLCleanField(
        required=False, allow_blank=True, max_length=DESCRIPTION_LENGTH
    )

    # Applicant attributes — gated by CallApplicantVisibilityConfig for reviewers.
    applicant_username = serializers.ReadOnlyField(source="created_by.username")
    applicant_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    applicant_first_name = serializers.ReadOnlyField(source="created_by.first_name")
    applicant_last_name = serializers.ReadOnlyField(source="created_by.last_name")
    applicant_email = serializers.ReadOnlyField(source="created_by.email")
    applicant_registration_method = serializers.ReadOnlyField(
        source="created_by.registration_method"
    )
    applicant_phone_number = serializers.ReadOnlyField(source="created_by.phone_number")
    applicant_organization = serializers.ReadOnlyField(source="created_by.organization")
    applicant_organization_country = serializers.ReadOnlyField(
        source="created_by.organization_country"
    )
    applicant_organization_type = serializers.ReadOnlyField(
        source="created_by.organization_type"
    )
    applicant_organization_registry_code = serializers.ReadOnlyField(
        source="created_by.organization_registry_code"
    )
    applicant_organization_vat_code = serializers.ReadOnlyField(
        source="created_by.organization_vat_code"
    )
    applicant_organization_address = serializers.ReadOnlyField(
        source="created_by.organization_address",
        allow_null=True,
    )
    applicant_job_title = serializers.ReadOnlyField(source="created_by.job_title")
    applicant_affiliations = serializers.ListField(
        child=serializers.CharField(),
        source="created_by.affiliations",
        read_only=True,
    )
    applicant_gender = serializers.ReadOnlyField(source="created_by.gender")
    applicant_personal_title = serializers.ReadOnlyField(
        source="created_by.personal_title"
    )
    applicant_place_of_birth = serializers.ReadOnlyField(
        source="created_by.place_of_birth"
    )
    applicant_address = serializers.ReadOnlyField(source="created_by.address")
    applicant_country_of_residence = serializers.ReadOnlyField(
        source="created_by.country_of_residence"
    )
    applicant_nationality = serializers.ReadOnlyField(source="created_by.nationality")
    applicant_nationalities = serializers.ListField(
        child=serializers.CharField(), source="created_by.nationalities", read_only=True
    )
    applicant_eduperson_assurance = serializers.ListField(
        child=serializers.CharField(),
        source="created_by.eduperson_assurance",
        read_only=True,
    )
    applicant_identity_source = serializers.ReadOnlyField(
        source="created_by.identity_source"
    )
    applicant_civil_number = serializers.ReadOnlyField(source="created_by.civil_number")
    applicant_birth_date = serializers.ReadOnlyField(source="created_by.birth_date")
    applicant_active_isds = serializers.ListField(
        child=serializers.CharField(), source="created_by.active_isds", read_only=True
    )

    # Compliance fields
    compliance_status = serializers.SerializerMethodField()
    can_submit = serializers.SerializerMethodField()
    awaiting_manual_advance = serializers.SerializerMethodField()
    decision_held = serializers.SerializerMethodField()
    open_change_requests = serializers.SerializerMethodField(
        help_text=(
            "The changes asked of the applicant and not yet submitted, earliest "
            "first: several steps of a stage may each ask at once. Each says "
            "what to change and which parts it unlocks meanwhile; the applicant "
            "may edit every part any of them unlocks, and submits them all at "
            "once. For the applicant team and those overseeing the call; empty "
            "otherwise."
        )
    )
    offering_team_providers = serializers.SerializerMethodField(
        help_text=(
            "The providers whose offering requested by the proposal the "
            "caller holds a role on -- its managers, or someone its part of "
            "a step can be handed to: each opens the proposal as that "
            "offering's team. Empty for everyone else."
        )
    )
    reads_own_offerings_only = serializers.SerializerMethodField(
        help_text=(
            "Whether the caller reads the proposal only as the provider of "
            "some of the offerings it requests: its requested resources, "
            "award, assessments and extensions then show only their own "
            "offerings' part."
        )
    )
    status_labels = serializers.SerializerMethodField(
        help_text=(
            "What the call configured the caller's dashboard to call the "
            "proposal's current stage: one label per step of the stage that "
            "has one for the caller (several when the stage runs steps side "
            "by side). Empty when nothing is configured, so the state stands."
        )
    )
    workflow_step = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Key of the call step the proposal stands in.",
    )
    requested_duration_months = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text=(
            "The project length the applicant chose from the call's "
            "allowed_duration_months. Written through update_project_details."
        ),
    )
    awarded_duration_months = serializers.SerializerMethodField(
        help_text=(
            "The project length the award grants, in months. Null for whoever "
            "may not read the award yet, as the awarded resources."
        )
    )
    completed_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="When the project was completed: its final report accepted.",
    )
    post_award_status = serializers.SerializerMethodField(
        help_text=(
            "Where an accepted proposal's extension request or final report "
            "stands. Null before acceptance, without post-award work, and "
            "for whoever may not read the award."
        )
    )
    open_run_uuid = serializers.SerializerMethodField(
        help_text="The open extension request or final report, if any."
    )
    open_run_phase = serializers.SerializerMethodField()
    extension_requestable_until = serializers.SerializerMethodField(
        help_text=(
            "The last day an extension can be requested; null when the call "
            "takes no extension requests or the project has no end date."
        )
    )
    final_report_openable = serializers.SerializerMethodField(
        help_text=(
            "Whether the final report can be opened now: the proposal is "
            "accepted with a project, not completed, has no open extension "
            "request or final report, and its call has final report steps. "
            "Who may open it is decided by the action itself."
        )
    )

    class Meta:
        model = models.Proposal
        fields = [
            "uuid",
            "url",
            "slug",
            "name",
            "description",
            "project_name",
            "project_summary",
            "supporting_documentation",
            "state",
            "approved_by",
            "created_by",
            "created_by_name",
            "created_by_uuid",
            # Applicant attributes (gated by CallApplicantVisibilityConfig)
            "applicant_username",
            "applicant_full_name",
            "applicant_first_name",
            "applicant_last_name",
            "applicant_email",
            "applicant_registration_method",
            "applicant_phone_number",
            "applicant_organization",
            "applicant_organization_country",
            "applicant_organization_type",
            "applicant_organization_registry_code",
            "applicant_organization_vat_code",
            "applicant_organization_address",
            "applicant_job_title",
            "applicant_affiliations",
            "applicant_gender",
            "applicant_personal_title",
            "applicant_place_of_birth",
            "applicant_address",
            "applicant_country_of_residence",
            "applicant_nationality",
            "applicant_nationalities",
            "applicant_eduperson_assurance",
            "applicant_identity_source",
            "applicant_civil_number",
            "applicant_birth_date",
            "applicant_active_isds",
            "project",
            "round",
            "round_uuid",
            "call_uuid",
            "call_name",
            "call_managing_organisation_uuid",
            "call_adoption_required",
            "oecd_fos_2007_code",
            "oecd_fos_2007_label",
            "science_sub_domain",
            "science_sub_domain_name",
            "science_domain_uuid",
            "science_domain_name",
            "panel_uuid",
            "panel_name",
            "allocation_comment",
            "created",
            "submitted_at",
            "compliance_status",
            "can_submit",
            "awaiting_manual_advance",
            "decision_held",
            "open_change_requests",
            "reads_own_offerings_only",
            "offering_team_providers",
            "workflow_step",
            "status_labels",
            "requested_duration_months",
            "awarded_duration_months",
            "completed_at",
            "post_award_status",
            "open_run_uuid",
            "open_run_phase",
            "extension_requestable_until",
            "final_report_openable",
        ]
        read_only_fields = (
            "workflow_step",
            "created_by",
            "approved_by",
            "project",
            "allocation_comment",
        )
        protected_fields = ("round_uuid",)
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "slug": {"required": False},
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "approved_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "project": {"lookup_field": "uuid", "view_name": "project-detail"},
        }

    def validate_name(self, value):
        # The proposal name is the applicant-controlled part of the project name
        # created on approval (see allocation_names), so hold it to the same
        # configurable pattern as the main project API. Validated on both create
        # and rename since the field-level check runs regardless of ``validate``.
        error = get_project_name_regex_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate(self, attrs):
        if self.instance:
            return attrs

        round_uuid = attrs.pop("round_uuid")

        try:
            call_round = models.Round.objects.get(uuid=round_uuid)
        except models.Round.DoesNotExist:
            raise serializers.ValidationError({"round_uuid": _("Round not found.")})

        if call_round.call.state != CallStates.ACTIVE:
            raise serializers.ValidationError(_("Call is not active."))

        # A proposal exists only while its round is open — it cannot be drafted
        # ahead of one opening, and cannot be started after the cutoff. Same
        # rule as submission, so a proposal can never be created into a state it
        # could not then be sent from.
        if call_round.status == RoundStatuses.SCHEDULED:
            raise serializers.ValidationError(
                _("Round has not opened yet, so a proposal cannot be created.")
            )
        if call_round.status == RoundStatuses.ENDED:
            raise serializers.ValidationError(
                _("Round has closed, so a proposal can no longer be created.")
            )

        attrs["round"] = call_round
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return data
        request = self.context.get("request")
        user = request.user if request else None
        # ``approved_by`` names the decision-maker (the call manager / staff who
        # accepted the proposal). Honour the call's blind-review setting: hide
        # it from the proposal's applicant team unless the call reveals reviewer
        # identity to submitters. Call team and staff (not the applicant team)
        # keep seeing it.
        if (
            user is not None
            and not getattr(user, "is_staff", False)
            and not instance.round.call.reviewer_identity_visible_to_submitters
            and proposal_roles.is_on_applicant_team(user, instance)
        ):
            data["approved_by"] = None
        if instance.panel_id is not None and not self._sees_panel(user, instance):
            data["panel_uuid"] = None
            data["panel_name"] = None
        # The allocation comment speaks to the whole award, every offering's
        # part of it: not a provider's, who reads only their own part.
        if data.get("allocation_comment") and not self._reads_whole(user, instance):
            data["allocation_comment"] = None
        return filter_applicant_fields_for_reviewer(data, instance, user)

    @extend_schema_field(OfferingTeamProviderSerializer(many=True))
    def get_offering_team_providers(self, obj):
        request = self.context.get("request")
        user = request.user if request else None
        if user is None or user.is_anonymous:
            return []
        # One query for the user's offering roles, and one for every
        # proposal of a listed page.
        offering_ids = self.context.get("_offering_team_ids")
        if offering_ids is None:
            offering_ids = set(contributions.offering_ids_with_role_of(user))
            self.context["_offering_team_ids"] = offering_ids
        if not offering_ids:
            return []
        cache = self.context.setdefault("_offering_team_providers", {})
        if obj.pk not in cache:
            batch = getattr(getattr(self, "parent", None), "instance", None)
            ids = {obj.pk}
            if isinstance(batch, list | tuple):
                ids |= {item.pk for item in batch}
            cache.update(
                contributions.offering_team_providers_by_proposal(ids, offering_ids)
            )
        return cache.get(obj.pk, [])

    def get_reads_own_offerings_only(self, obj) -> bool:
        request = self.context.get("request")
        user = request.user if request else None
        if user is None or user.is_anonymous:
            return False
        return not self._reads_whole(user, obj)

    def _reads_whole(self, user, proposal) -> bool:
        """Whether ``user`` reads all of ``proposal``, not only a provider's
        part of it (``permissions.offering_scope``). Answered for every
        proposal of a listed page at once."""
        if user is None or user.is_anonymous:
            return False
        if user.is_staff or user.is_support:
            return True
        cache = self.context.setdefault("_reads_whole_proposal", {})
        if proposal.pk not in cache:
            batch = getattr(getattr(self, "parent", None), "instance", None)
            ids = {proposal.pk}
            if isinstance(batch, list | tuple):
                ids |= {item.pk for item in batch}
            whole = set(
                proposal_permissions.proposals_read_whole(user)
                .filter(pk__in=ids)
                .values_list("pk", flat=True)
            )
            for pk in ids:
                cache.setdefault(pk, pk in whole)
        return cache[proposal.pk]

    def _sees_panel(self, user, proposal) -> bool:
        """Whether ``user`` sees which panel evaluates ``proposal``."""
        # One per listing, so its memos span every row.
        cached = self.context.get("_panel_visibility")
        if cached is None or cached.user != user:
            cached = self.context["_panel_visibility"] = PanelVisibility(user)
        return cached.sees(proposal)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")

        # Only allow staff to edit slug field
        if request and not (request.user and request.user.is_staff):
            if "slug" in fields:
                fields["slug"].read_only = True

        return fields

    @extend_schema_field(ProposalComplianceStatusSerializer(allow_null=True))
    def get_compliance_status(self, obj):
        """Get compliance checklist status."""
        if not obj.round.call.compliance_checklist:
            return None

        # None where the checklist was added to the call after the proposal
        # was created: nothing answered yet. ``checklist_completion`` is a
        # property, so a hasattr() test never caught it.
        completion = obj.checklist_completion
        if completion is None:
            return {
                "error": "Compliance checklist not initialized",
                "has_checklist": True,
                "is_completed": False,
                "requires_review": False,
                "completion_percentage": 0,
            }

        return {
            "has_checklist": True,
            "is_completed": completion.is_completed,
            "requires_review": completion.requires_review,
            "completion_percentage": completion.get_completion_percentage(),
            "reviewed_by": completion.reviewed_by.full_name
            if completion.reviewed_by
            else None,
            "reviewed_at": completion.reviewed_at,
            "checklist_name": completion.checklist.name,
            "unanswered_required_count": completion.get_unanswered_required_questions().count(),
        }

    @extend_schema_field(ProposalCanSubmitResponseSerializer)
    def get_can_submit(self, obj):
        """Whether the proposal can be submitted -- or, while changes are asked
        of the applicant, whether the changes can: those are checked only in
        the parts the requests unlock (``Proposal.can_submit_changes``)."""
        pending = self._pending_changes(obj)
        if pending and pending[0].step_instance.run_id is None:
            can_submit, error = obj.can_submit_changes(
                {part for request in pending for part in request.unlocked_parts or []}
            )
        else:
            can_submit, error = obj.can_submit()
        return {"can_submit": can_submit, "error": error}

    def _pending_changes(self, obj):
        """The open requests for changes to the applicant on ``obj``, read
        once for ``can_submit`` and ``open_change_requests`` alike."""
        if obj.state not in (ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED):
            return []
        prefetched = getattr(obj, "_open_change_requests", None)
        if prefetched is None:
            prefetched = list(change_requests.open_applicant_requests(obj))
            obj._open_change_requests = prefetched
        return prefetched

    @extend_schema_field(serializers.BooleanField())
    def get_awaiting_manual_advance(self, obj) -> bool:
        """True iff the current step is completed and awaiting a manual advance.

        Prefers the queryset annotations (set by ProposalViewSet.get_queryset)
        to avoid a per-row query on list. Falls back to the DB-fresh service
        helper for objects serialized outside that viewset (e.g. after a
        mutation).
        """
        if hasattr(obj, "_awaiting_manual_step"):
            return bool(
                obj.workflow_step
                and not obj.decision_held
                and obj._awaiting_manual_step
                and obj._latest_step_status == WorkflowStepInstanceStatuses.COMPLETED
            )
        return workflow_service.is_awaiting_manual_advance(obj)

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_decision_held(self, obj) -> bool | None:
        """Whether the allocation decision is made but held for the round.

        Null for anyone but the call managers (and staff and support): to
        everyone else a held decision is still being made, and a false here
        would tell them apart.
        """
        request = self.context.get("request")
        if not request:
            return None
        # The answer depends on the call and on whether the user is on the
        # applicant team, so a list checks once per call, not per proposal.
        cache = self.context.setdefault("_held_decision_visible", {})
        if "_team_proposal_ids" not in self.context:
            self.context["_team_proposal_ids"] = set(
                proposal_roles.applicant_team_proposal_ids(request.user)
            )
        key = (obj.round.call_id, obj.pk in self.context["_team_proposal_ids"])
        if key not in cache:
            cache[key] = proposal_permissions.user_can_view_held_decision(
                request.user, obj
            )
        return obj.decision_held if cache[key] else None

    @extend_schema_field(ProposalStatusLabelSerializer(many=True))
    def get_status_labels(self, obj):
        request = self.context.get("request")
        if not request:
            return []
        instances = getattr(obj, "_labelled_steps", None)
        if instances is None:
            instances = list(
                obj.workflow_step_instances.filter(
                    status__in=(
                        WorkflowStepInstanceStatuses.ACTIVE,
                        WorkflowStepInstanceStatuses.COMPLETED,
                    )
                )
                .exclude(call_step__status_labels={})
                .select_related("call_step")
                .order_by("order", "id")
            )
        if not instances:
            return []
        active = [
            i for i in instances if i.status == WorkflowStepInstanceStatuses.ACTIVE
        ]
        if active:
            stage = active[0].stage_key
        elif obj.current_step_id:
            stage = obj.current_step.stage_key
        else:
            return []
        reader = self._status_label_reader(request.user, obj)
        if reader is None:
            return []
        access = self.context.get("_label_access")
        if access is None or access.user != request.user:
            access = self.context["_label_access"] = contributions.Access(request.user)
        counts_cache = self.context.setdefault("_step_counts", {})
        withheld = bool(obj.decision_held) and self.get_decision_held(obj) is None
        changes_requested_at = {
            change_request.get("step_key")
            for change_request in self.get_open_change_requests(obj)
        }
        result = []
        for instance in instances:
            if instance.stage_key != stage:
                continue
            call_step = instance.call_step
            status = instance.status
            outcome = instance.outcome
            if instance.is_allocation_decision and withheld:
                status, outcome = WorkflowStepInstanceStatuses.ACTIVE, None
            elif reader in ("evaluator", "outside"):
                # Outcomes are the call team's; an evaluator reads them only
                # on the steps they take part in, on the step list.
                outcome = None
            elif reader == "applicant" and (
                ProposalWorkflowStepInstanceSerializer._is_review_step(instance)
                and not obj.round.call.reviews_visible_to_submitters
            ):
                outcome = None
            instance.proposal = obj
            audience = ProposalWorkflowStepInstanceSerializer.label_audience(
                request.user, instance, access, side=reader
            )
            if audience is None:
                continue
            label = step_status_labels.pick(
                step_status_labels.configured(call_step, audience),
                status,
                outcome,
                changes_requested=instance.step in changes_requested_at,
            )
            label = step_status_labels.render(
                label,
                lambda instance=instance: (
                    ProposalWorkflowStepInstanceSerializer.step_counts(
                        instance, counts_cache
                    )
                ),
            )
            if label:
                result.append({"step": instance.step, "label": label})
        return result

    def _status_label_reader(self, user, proposal):
        """Which side ``user`` reads the proposal list from: "applicant",
        "call_team" (every outcome), "evaluator" (no outcomes), "outside" (a
        provider: labels of the steps they work on, no outcomes), or None.
        ``ProposalWorkflowStepInstanceSerializer.label_audience`` picks the
        labels' audience from it, step by step."""
        if "_team_proposal_ids" not in self.context:
            self.context["_team_proposal_ids"] = set(
                proposal_roles.applicant_team_proposal_ids(user)
            )
        if proposal.pk in self.context["_team_proposal_ids"]:
            return "applicant"
        call = proposal.round.call
        cache = self.context.setdefault("_status_label_readers", {})
        if call.pk not in cache:
            if (
                user.is_staff
                or user.is_support
                or proposal_permissions.user_can_view_internal_notes(user, proposal)
                or proposal_permissions.oversees_proposal_call(user, proposal)
            ):
                cache[call.pk] = "call_team"
            elif permissions_utils.has_user(call, user):
                cache[call.pk] = "evaluator"
            else:
                cache[call.pk] = None
        if cache[call.pk] is not None:
            return cache[call.pk]
        if "_held_proposal_ids" not in self.context:
            self.context["_held_proposal_ids"] = set(
                proposal_roles.proposals_held_by(user)
            )
        if proposal.pk in self.context["_held_proposal_ids"]:
            return "evaluator"
        if "_managed_offering_ids" not in self.context:
            self.context["_managed_offering_ids"] = bool(
                contributions.offering_ids_managed_by(user)
            )
        if self.context["_managed_offering_ids"]:
            return "outside"
        return None

    @extend_schema_field(ProposalChangeRequestSerializer(many=True))
    def get_open_change_requests(self, obj):
        # In review, or accepted with the request made in a post-award run.
        pending = self._pending_changes(obj)
        if not pending:
            return []
        request = self.context.get("request")
        if not request:
            return []
        user = request.user
        if "_team_proposal_ids" not in self.context:
            self.context["_team_proposal_ids"] = set(
                proposal_roles.applicant_team_proposal_ids(user)
            )
        on_team = obj.pk in self.context["_team_proposal_ids"]
        if not (on_team or user.is_staff or user.is_support):
            cache = self.context.setdefault("_oversees_call", {})
            call_id = obj.round.call_id
            if call_id not in cache:
                cache[call_id] = proposal_permissions.oversees_proposal_call(user, obj)
            if not cache[call_id]:
                return []
        return ProposalChangeRequestSerializer(
            pending,
            many=True,
            context={"conceals_requester": (lambda _: on_team)},
        ).data

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_awarded_duration_months(self, obj) -> int | None:
        if obj.awarded_duration_months is None:
            return None
        request = self.context.get("request")
        if not request or not proposal_permissions.user_can_view_awarded_resources(
            request.user, obj
        ):
            return None
        return obj.awarded_duration_months

    def _reads_runs(self, obj) -> bool:
        if obj.state != ProposalStates.ACCEPTED:
            return False
        request = self.context.get("request")
        return bool(
            request and proposal_permissions.user_can_view_runs(request.user, obj)
        )

    @extend_schema_field(
        serializers.ChoiceField(choices=PostAwardStatuses.CHOICES, allow_null=True)
    )
    def get_post_award_status(self, obj):
        if not self._reads_runs(obj):
            return None
        return runs.post_award_status(obj)

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_open_run_uuid(self, obj):
        if not self._reads_runs(obj):
            return None
        if hasattr(obj, "_open_run_uuid"):
            value = obj._open_run_uuid
            return value.hex if value else None
        run = runs.open_run_of(obj)
        return run.uuid.hex if run else None

    @extend_schema_field(
        serializers.ChoiceField(choices=WorkflowPhases.CHOICES, allow_null=True)
    )
    def get_open_run_phase(self, obj):
        if not self._reads_runs(obj):
            return None
        if hasattr(obj, "_open_run_phase"):
            return obj._open_run_phase
        run = runs.open_run_of(obj)
        return run.phase if run else None

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_extension_requestable_until(self, obj):
        if not self._reads_runs(obj) or obj.completed_at is not None:
            return None
        if runs.offset_days(obj, WorkflowPhases.EXTENSION) is None:
            return None
        return runs.window_end(obj)

    def get_final_report_openable(self, obj) -> bool:
        if not self._reads_runs(obj):
            return False
        return runs.open_refusal(obj, WorkflowPhases.CLOSING) is None


class RoundReviewerSerializer(serializers.Serializer):
    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    accepted_proposals = serializers.IntegerField()
    rejected_proposals = serializers.IntegerField()
    in_review_proposals = serializers.IntegerField()

    def get_full_name(self, obj) -> str:
        return f"{obj.first_name} {obj.last_name}"


class CallRoundSerializer(serializers.HyperlinkedModelSerializer):
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.Round
        fields = [
            "url",
            "uuid",
            "slug",
            "start_time",
            "cutoff_time",
            "call_uuid",
            "call_name",
            "status",
        ]
        extra_kwargs = {
            "slug": {"required": False},
            "url": {
                "lookup_field": "uuid",
                "view_name": "call-round-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-public-call-detail",
            },
        }


class CallManagingOrganisationStatSerializer(serializers.Serializer):
    open_calls = serializers.IntegerField(read_only=True)
    active_rounds = serializers.IntegerField(read_only=True)
    accepted_proposals = serializers.IntegerField(read_only=True)
    pending_proposals = serializers.IntegerField(read_only=True)
    pending_review = serializers.IntegerField(read_only=True)
    rounds_closing_in_one_week = serializers.IntegerField(read_only=True)
    calls_closing_in_one_week = serializers.IntegerField(read_only=True)
    offering_requests_pending = serializers.IntegerField(read_only=True)


class CallStatsStepSerializer(serializers.Serializer):
    step = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField()


class CallStatsBlockSerializer(serializers.Serializer):
    proposals = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Proposals by state (accepted = awarded), plus total.",
    )
    reviews = serializers.DictField(
        child=serializers.IntegerField(), help_text="Reviews by state, plus total."
    )
    admin_approved = serializers.IntegerField(
        help_text="Proposals whose administrative check completed as eligible."
    )
    missing_admin_checks = serializers.IntegerField(
        help_text="Proposals whose administrative check is active or expired."
    )
    missing_reviews = serializers.IntegerField(
        help_text="Proposals in expert review below the step's minimum reviews."
    )
    overdue_reviews = serializers.IntegerField(
        help_text="Reviews still in review past their deadline."
    )
    steps = CallStatsStepSerializer(
        many=True,
        help_text="Proposals per current workflow step, over the enabled steps.",
    )


class CallStatsRoundSerializer(CallStatsBlockSerializer):
    uuid = serializers.UUIDField()
    slug = serializers.CharField()
    start_time = serializers.DateTimeField()
    cutoff_time = serializers.DateTimeField()


class CallStatsSerializer(serializers.Serializer):
    call = CallStatsBlockSerializer()
    rounds = CallStatsRoundSerializer(many=True)


class ReportFilterSerializer(serializers.Serializer):
    """The filters every call management report takes."""

    call_uuid = serializers.ListField(child=serializers.UUIDField(), required=False)
    organisation_uuid = serializers.ListField(
        child=serializers.UUIDField(), required=False
    )
    round_uuid = serializers.UUIDField(required=False)
    submitted_from = serializers.DateField(required=False)
    submitted_to = serializers.DateField(required=False)

    def validate(self, attrs):
        start, end = attrs.get("submitted_from"), attrs.get("submitted_to")
        if start and end and start > end:
            raise serializers.ValidationError(
                {"submitted_to": _("The end date is before the start date.")}
            )
        return attrs


class PortfolioReportBlockSerializer(serializers.Serializer):
    proposals = serializers.IntegerField(
        help_text="Proposals counted: those submitted within the dates when "
        "dates are given, else every proposal, drafts included."
    )
    submitted = serializers.IntegerField(
        help_text="Proposals that left draft: they have a submission time."
    )
    draft = serializers.IntegerField()
    awaiting_review = serializers.IntegerField(
        help_text="Submitted, not yet taken into evaluation."
    )
    in_review = serializers.IntegerField()
    accepted = serializers.IntegerField()
    rejected = serializers.IntegerField()
    canceled = serializers.IntegerField(help_text="Withdrawn or canceled.")
    decided = serializers.IntegerField(help_text="Accepted plus rejected.")
    success_rate = serializers.FloatField(
        allow_null=True,
        help_text="Accepted out of decided, 0-1; empty while none is decided.",
    )
    scored_reviews = serializers.IntegerField(
        help_text="Submitted reviews with an overall score."
    )
    mean_score = serializers.FloatField(
        allow_null=True,
        help_text="Mean overall score of the submitted reviews, 1-5.",
    )
    active_rounds = serializers.IntegerField(
        help_text="Rounds open for submissions now: within their submission "
        "window, of an active call."
    )


class PortfolioReportRoundSerializer(PortfolioReportBlockSerializer):
    round_uuid = serializers.UUIDField()
    round_name = serializers.CharField()
    round_slug = serializers.CharField()
    start_time = serializers.DateTimeField()
    cutoff_time = serializers.DateTimeField(allow_null=True)


class PortfolioReportCallSerializer(PortfolioReportBlockSerializer):
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    call_slug = serializers.CharField()
    organisation_uuid = serializers.UUIDField(
        help_text="The call's call managing organization."
    )
    organisation_name = serializers.CharField(
        help_text="The name of the call managing organization's customer."
    )
    call_state = serializers.CharField()
    rounds = PortfolioReportRoundSerializer(many=True)


class PortfolioReportMonthSerializer(serializers.Serializer):
    month = serializers.DateField(help_text="The first day of the month.")
    submitted = serializers.IntegerField()
    accepted = serializers.IntegerField(
        help_text="Of the proposals submitted that month, those accepted."
    )


class PortfolioReportSerializer(serializers.Serializer):
    totals = PortfolioReportBlockSerializer()
    calls = PortfolioReportCallSerializer(many=True)
    months = PortfolioReportMonthSerializer(many=True)


class WorkflowReportFiguresSerializer(serializers.Serializer):
    total = serializers.IntegerField(help_text="Step instances counted.")
    pending = serializers.IntegerField(help_text="Not started yet.")
    waiting = serializers.IntegerField(help_text="Proposals at the step now.")
    overdue = serializers.IntegerField(
        help_text="Of those waiting, the ones past the step's deadline."
    )
    completed = serializers.IntegerField()
    completed_late = serializers.IntegerField(
        help_text="Completed after the step's deadline."
    )
    skipped = serializers.IntegerField()
    expired = serializers.IntegerField()
    withdrawn = serializers.IntegerField()
    median_days = serializers.FloatField(
        allow_null=True, help_text="Median days from start to completion."
    )
    p90_days = serializers.FloatField(
        allow_null=True,
        help_text="90th percentile of the days from start to completion.",
    )


class WorkflowReportCallStepSerializer(WorkflowReportFiguresSerializer):
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    call_slug = serializers.CharField()
    organisation_uuid = serializers.UUIDField(
        help_text="The call's call managing organization."
    )
    organisation_name = serializers.CharField(
        help_text="The name of the call managing organization's customer."
    )
    name = serializers.CharField(help_text="The step's name in this call.")
    enabled = serializers.BooleanField()
    planned_days = serializers.IntegerField(allow_null=True)


class WorkflowReportStepSerializer(WorkflowReportFiguresSerializer):
    key = serializers.CharField()
    name = serializers.CharField()
    phase = serializers.CharField()
    calls = WorkflowReportCallStepSerializer(
        many=True, help_text="The step in each call that has it."
    )


class WorkflowReportSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    proposals = serializers.IntegerField(
        help_text="Proposals with a step instance in the scope."
    )
    steps = WorkflowReportStepSerializer(
        many=True,
        help_text="Per step key, summed over the calls, in workflow order.",
    )


class ReviewerReportCountsSerializer(serializers.Serializer):
    invitations = serializers.IntegerField(
        help_text="Proposals sent to the reviewer in assignment batches; a "
        "draft batch's are not counted until it is sent. The sum of the "
        "invitations_* counts."
    )
    invitations_accepted = serializers.IntegerField()
    invitations_declined = serializers.IntegerField()
    invitations_expired = serializers.IntegerField(
        help_text="Left unanswered until the batch's deadline."
    )
    invitations_pending = serializers.IntegerField(
        help_text="Awaiting an answer, before the batch's deadline."
    )
    invitations_withdrawn = serializers.IntegerField(
        help_text="Unanswered when the call team canceled the batch."
    )
    invitations_coi_blocked = serializers.IntegerField(
        help_text="Blocked by a conflict of interest."
    )
    invitations_reassigned = serializers.IntegerField(
        help_text="Given to another reviewer instead."
    )
    reviews_assigned = serializers.IntegerField()
    reviews_submitted = serializers.IntegerField()
    reviews_in_progress = serializers.IntegerField(
        help_text="In review, with a score or a comment entered."
    )
    reviews_not_started = serializers.IntegerField(
        help_text="In review, with nothing entered yet."
    )
    reviews_declined = serializers.IntegerField()
    reviews_expired = serializers.IntegerField(
        help_text="Closed at their deadline without being submitted."
    )
    reviews_withdrawn = serializers.IntegerField(
        help_text="Withdrawn when the reviewer joined the proposal's team."
    )
    reviews_overdue = serializers.IntegerField(
        help_text="In review past their deadline."
    )


class ReviewerReportRowSerializer(ReviewerReportCountsSerializer):
    reviewer_uuid = serializers.UUIDField(
        allow_null=True,
        help_text="Empty for a reviewer invited by email who has no account "
        "yet: the row is their address's invitations.",
    )
    reviewer_name = serializers.CharField(allow_blank=True)
    reviewer_email = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField(
        allow_null=True, help_text="Empty without an account."
    )
    median_response_days = serializers.FloatField(
        allow_null=True,
        help_text="Median days from an assignment batch being sent to the "
        "reviewer accepting or declining a proposal of it.",
    )
    median_turnaround_days = serializers.FloatField(
        allow_null=True,
        help_text="Median days from a review's creation to its submission, "
        "over the reviews whose submission time is recorded.",
    )
    mean_score = serializers.FloatField(
        allow_null=True,
        help_text="Mean overall score of the submitted reviews, 1-5.",
    )
    last_submitted_at = serializers.DateTimeField(allow_null=True)


class ReviewerReportTotalsSerializer(ReviewerReportCountsSerializer):
    reviewers = serializers.IntegerField()


class ReviewerReportSerializer(serializers.Serializer):
    totals = ReviewerReportTotalsSerializer()
    reviewers = ReviewerReportRowSerializer(
        many=True, help_text="Busiest first: by reviews assigned."
    )


class RoundReportRowSerializer(serializers.Serializer):
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    call_slug = serializers.CharField()
    organisation_uuid = serializers.UUIDField(
        help_text="The call's call managing organization."
    )
    organisation_name = serializers.CharField(
        help_text="The name of the call managing organization's customer."
    )
    round_uuid = serializers.UUIDField()
    round_name = serializers.CharField()
    round_slug = serializers.CharField()
    call_state = serializers.CharField()
    status = serializers.ChoiceField(
        choices=["scheduled", "open", "ended", "call_archived", "call_draft"],
        help_text="Where the round stands by its submission window; "
        "call_archived or call_draft for every round of an archived or a "
        "draft call, which takes no submissions.",
    )
    lifecycle_state = serializers.CharField(
        allow_null=True, help_text="Where the round stands after its cut-off."
    )
    proposals_submitted = serializers.IntegerField()
    start_time = serializers.DateTimeField()
    cutoff_time = serializers.DateTimeField(allow_null=True)
    evaluation_started_at = serializers.DateTimeField(allow_null=True)
    deciding_started_at = serializers.DateTimeField(allow_null=True)
    results_published_at = serializers.DateTimeField(allow_null=True)
    adopted_at = serializers.DateField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    allocation_date = serializers.DateTimeField(allow_null=True)
    days_cutoff_to_evaluation = serializers.FloatField(allow_null=True)
    days_evaluating = serializers.FloatField(
        allow_null=True, help_text="From the evaluation start to deciding."
    )
    days_deciding = serializers.FloatField(
        allow_null=True, help_text="From deciding to the results' publication."
    )
    days_published_to_adopted = serializers.FloatField(
        allow_null=True,
        help_text="Whole days from the day the results were published to the "
        "day of their adoption. Negative when they were adopted first, as a "
        "call that requires adoption has them: it publishes only adopted "
        "results.",
    )
    days_cutoff_to_published = serializers.FloatField(allow_null=True)
    days_after_allocation_date = serializers.IntegerField(
        allow_null=True,
        help_text="Whole days from the allocation date to the day the results "
        "were published (negative: early); to today while unpublished past "
        "the date. Empty without an allocation date, or before it.",
    )
    late = serializers.BooleanField(
        help_text="Results published on a day after the allocation date, or "
        "still unpublished past it."
    )
    current_stage = serializers.ChoiceField(
        choices=[
            "scheduled",
            "open",
            "awaiting_evaluation",
            "evaluating",
            "deciding",
            "results_published",
            "call_archived",
            "call_draft",
        ],
        allow_null=True,
        help_text="Where a round not closed yet stands: not open yet, open "
        "for submissions, past its cut-off with its evaluation not started, "
        "evaluating, deciding, or with its results published; call_archived "
        "or call_draft for a round of an archived or a draft call. Empty once "
        "closed.",
    )
    days_in_current_stage = serializers.FloatField(
        allow_null=True,
        help_text="Days since the round entered its current stage, to now. "
        "Empty for a closed round, one not open yet, and one of a call not "
        "active.",
    )


class RoundReportTotalsSerializer(serializers.Serializer):
    rounds = serializers.IntegerField()
    published = serializers.IntegerField(
        help_text="Rounds whose results are published, after a cut-off."
    )
    late = serializers.IntegerField()
    median_days_cutoff_to_published = serializers.FloatField(allow_null=True)


class RoundReportSerializer(serializers.Serializer):
    totals = RoundReportTotalsSerializer()
    rounds = RoundReportRowSerializer(many=True)


class BudgetReportPoolSerializer(serializers.Serializer):
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    call_slug = serializers.CharField()
    organisation_uuid = serializers.UUIDField(
        help_text="The call's call managing organization."
    )
    organisation_name = serializers.CharField(
        help_text="The name of the call managing organization's customer."
    )
    round_uuid = serializers.UUIDField(
        allow_null=True, help_text="Empty: the whole call."
    )
    round_name = serializers.CharField(allow_null=True)
    pool_uuid = serializers.UUIDField(
        allow_null=True,
        help_text="Empty: no pool, only what was awarded or asked for.",
    )
    within_call_pool = serializers.BooleanField(
        help_text="A round's pool whose awards also count against the "
        "call-wide pool listed in another row: leave it out of sums. A "
        "call-wide pool is listed with its whole call's figures also when the "
        "report is filtered to a round."
    )
    offering_name = serializers.CharField()
    component_type = serializers.CharField()
    component_name = serializers.CharField(allow_blank=True)
    measured_unit = serializers.CharField(allow_blank=True)
    limit_period = serializers.CharField(allow_blank=True)
    offered_total = LimitValueField(allow_null=True)
    requested = LimitValueField(
        help_text="Asked for by submitted proposals, whatever came of them."
    )
    awarded = LimitValueField(
        help_text="What the allocation decisions granted, over the whole "
        "length of each award."
    )
    remaining = LimitValueField(allow_null=True)
    undecided = LimitValueField(
        help_text="Asked for by proposals still waiting for their decision."
    )
    awarded_share = serializers.FloatField(
        allow_null=True, help_text="Awarded out of offered, 0-1."
    )


class BudgetReportTotalsSerializer(serializers.Serializer):
    pools = serializers.IntegerField()
    pools_exhausted = serializers.IntegerField(
        help_text="Pools with nothing left to award."
    )


class BudgetReportSerializer(serializers.Serializer):
    totals = BudgetReportTotalsSerializer()
    pools = BudgetReportPoolSerializer(many=True)


class CallAttachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.FileField())
    description = serializers.CharField(required=False)


class CallDetachDocumentsSerializer(serializers.Serializer):
    documents = serializers.ListField(child=serializers.UUIDField())


class ProposalProjectRoleMappingSerializer(serializers.HyperlinkedModelSerializer):
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    proposal_role = serializers.SlugRelatedField(
        slug_field="name",
        queryset=Role.objects.filter(is_active=True, content_type__model="proposal"),
        required=True,
        allow_null=False,
    )
    project_role = serializers.SlugRelatedField(
        slug_field="name",
        queryset=Role.objects.filter(is_active=True, content_type__model="project"),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.ProposalProjectRoleMapping
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "proposal_role",
            "project_role",
        ]
        read_only_fields = ("uuid",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "call-proposal-project-role-mapping-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }

    def validate(self, attrs):
        if self.instance:
            call = self.instance.call
        else:
            call = attrs["call"]
        if not permissions_utils.has_permission_on_any_source(
            self.context["request"],
            permissions_enums.PermissionEnum.UPDATE_CALL,
            call,
            proposal_permissions.CALL_PERMISSION_SOURCES,
        ):
            raise PermissionDenied()

        if call.state == CallStates.ARCHIVED:
            raise serializers.ValidationError(_("Cannot modify an archived call."))

        return attrs

    def get_fields(self):
        fields = super().get_fields()
        # Make only project_role updatable if instance exists
        if hasattr(self, "instance") and self.instance:
            fields["proposal_role"].read_only = True
            fields["call"].read_only = True
        return fields


# Checklist Integration Serializers
# Backward compatibility aliases - use generic serializers from checklist app
ProposalChecklistCompletionSerializer = (
    checklist_serializers.ChecklistCompletionSerializer
)
ProposalChecklistAnswerSubmitSerializer = checklist_serializers.AnswerSubmitSerializer


class ProposalChecklistAnswerSubmitResponseSerializer(serializers.Serializer):
    """Custom response serializer for proposal answer submission that includes review status."""

    detail = serializers.CharField()
    completion = checklist_serializers.ChecklistCompletionReviewerSerializer()


def _file_label(item) -> str | None:
    """An attached file as its name and size, the way a reader names it."""
    if not isinstance(item, dict) or not item.get("name"):
        return None
    size = item.get("size")
    if isinstance(size, int) and not isinstance(size, bool):
        return f"{item['name']} ({filesizeformat(size)})"
    return str(item["name"])


def _scalar_display(value) -> str | None:
    """A plain answer as text. A structured value has no reading of its own
    and is left out rather than shown as its Python representation."""
    if value in (None, "") or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return str(_("Yes") if value else _("No"))
    return str(value)


class TechnicalAssessmentAnswerSerializer(serializers.Serializer):
    """One reviewer's answer to a technical-assessment question, with a
    human-readable ``answer_display``: option labels for select questions,
    file names and sizes for file questions."""

    question_uuid = serializers.UUIDField(source="question.uuid")
    question_description = serializers.CharField(source="question.description")
    question_type = serializers.CharField(source="question.question_type")
    answer_data = serializers.JSONField()
    answer_display = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_answer_display(self, obj):
        question = obj.question
        value = obj.answer_data
        if question.question_type in ("single_select", "multi_select"):
            option_uuids = value if isinstance(value, list) else [value]
            labels = list(
                checklist_models.QuestionOption.objects.filter(
                    question=question, uuid__in=[str(u) for u in option_uuids]
                )
                .order_by("order")
                .values_list("label", flat=True)
            )
            return ", ".join(labels) if labels else None
        if question.question_type in (
            checklist_enums.QuestionTypes.FILE,
            checklist_enums.QuestionTypes.MULTIPLE_FILES,
        ):
            files = value if isinstance(value, list) else [value]
            labels = [_file_label(item) for item in files]
            return ", ".join(label for label in labels if label) or None
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        if value in (None, ""):
            return None
        if isinstance(value, list):
            items = [_scalar_display(item) for item in value]
            return ", ".join(item for item in items if item) or None
        return _scalar_display(value)


class ContributionDelegationCandidateSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    full_name = serializers.CharField()


class StepContributionSerializer(serializers.Serializer):
    """One contribution to a proposal's workflow step.

    The contributor's identity is hidden from the applicant unless the call
    reveals reviewer identity to submitters, as on the review responses.
    """

    uuid = serializers.UUIDField(format="hex")
    proposal_step_uuid = serializers.UUIDField(
        source="proposal_step.uuid", format="hex"
    )
    call_step_uuid = serializers.UUIDField(
        source="proposal_step.call_step.uuid", format="hex"
    )
    step_name = serializers.CharField(source="proposal_step.call_step.name")
    is_per_offering = serializers.BooleanField()
    requested_offering_uuid = serializers.SerializerMethodField()
    offering_uuid = serializers.SerializerMethodField()
    offering_name = serializers.SerializerMethodField()
    contributor_uuid = serializers.SerializerMethodField()
    contributor_full_name = serializers.SerializerMethodField()
    is_lead = serializers.BooleanField()
    status = serializers.ChoiceField(choices=StepContributionStatuses.CHOICES)
    outcome = serializers.CharField(allow_blank=True)
    conditions = serializers.CharField(allow_blank=True)
    comment = serializers.CharField(allow_blank=True)
    review_uuid = serializers.SerializerMethodField()
    due_at = serializers.DateTimeField(allow_null=True)
    submitted_at = serializers.DateTimeField(allow_null=True)
    can_fill = serializers.SerializerMethodField()
    checklist_completed = serializers.SerializerMethodField()
    unanswered_required_count = serializers.SerializerMethodField()
    delegate_uuid = serializers.SerializerMethodField(
        help_text=(
            "Who the slot is handed to, while they may take it over; hidden "
            "from whoever may not know who contributed."
        )
    )
    delegate_full_name = serializers.SerializerMethodField()
    delegated_at = serializers.SerializerMethodField()
    can_delegate = serializers.SerializerMethodField(
        help_text="Whether the caller may hand the slot to someone, or take it back."
    )
    can_accept = serializers.SerializerMethodField(
        help_text="Whether the slot is handed to the caller, who has yet to accept it."
    )
    coi_declaration_text = serializers.SerializerMethodField(
        help_text=(
            "The conflict-of-interest declaration taking the slot over asks "
            "of the caller, while it is handed to them; otherwise the one its "
            "delegate confirmed. Empty for the platform's standard "
            "declaration, or when none is asked for."
        )
    )
    delegation_candidates = serializers.SerializerMethodField(
        help_text=(
            "Who the caller may hand the slot to, when they may hand it on; "
            "empty otherwise."
        )
    )

    def _access(self):
        request = self.context.get("request")
        return contributions.access_for(request) if request else None

    def _anonymize(self, obj):
        access = self._access()
        return bool(access) and access.conceals_contributor(obj)

    def _conceals_applicant(self, obj, attribute) -> bool:
        """Whether ``attribute`` of a contributor on the applicant team -- the
        author of a response, say -- is concealed from an evaluator by the
        call's applicant visibility config, as on the proposal itself."""
        request = self.context.get("request")
        if not request or not obj.contributor_id:
            return False
        proposal = obj.proposal_step.proposal
        concealed = get_applicant_visibility(
            self.context, request.user
        ).concealed_attributes(proposal)
        if not concealed or attribute not in concealed:
            return False
        team = self.context.setdefault("_applicant_team_ids", {})
        if proposal.pk not in team:
            team[proposal.pk] = proposal_roles.applicant_team_user_ids(proposal)
        return obj.contributor_id in team[proposal.pk]

    @extend_schema_field(serializers.UUIDField(allow_null=True, format="hex"))
    def get_requested_offering_uuid(self, obj):
        return obj.requested_offering.uuid.hex if obj.requested_offering_id else None

    @extend_schema_field(serializers.UUIDField(allow_null=True, format="hex"))
    def get_offering_uuid(self, obj):
        if not obj.requested_offering_id:
            return None
        return obj.requested_offering.offering.uuid.hex

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_offering_name(self, obj):
        if not obj.requested_offering_id:
            return None
        return obj.requested_offering.offering.name

    @extend_schema_field(serializers.UUIDField(allow_null=True, format="hex"))
    def get_contributor_uuid(self, obj):
        if (
            not obj.contributor_id
            or self._anonymize(obj)
            or self._conceals_applicant(obj, "username")
        ):
            return None
        return obj.contributor.uuid.hex

    def _anonymous_label(self, obj):
        labels = self.context.setdefault("_anonymous_labels", {})
        proposal = obj.proposal_step.proposal
        if proposal.pk not in labels:
            labels[proposal.pk] = contributions.anonymous_labels(proposal)
        return contributions.anonymous_label(obj, labels[proposal.pk])

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_contributor_full_name(self, obj):
        if not obj.contributor_id:
            return None
        if self._anonymize(obj):
            return self._anonymous_label(obj)
        if self._conceals_applicant(obj, "full_name"):
            return str(_("Applicant"))
        return obj.contributor.full_name

    @extend_schema_field(serializers.UUIDField(allow_null=True, format="hex"))
    def get_review_uuid(self, obj):
        return obj.review.uuid.hex if obj.review_id else None

    def _shown_delegate(self, obj):
        """The person the slot is handed to, as the caller may know them, or
        None: concealed on the terms its contributor is."""
        access = self._access()
        if not access:
            return None
        delegate_id = access.delegate_id(obj)
        if delegate_id is None:
            return None
        if delegate_id != access.user.pk and access.conceals_contributor(obj):
            return None
        return obj.delegate

    @extend_schema_field(serializers.UUIDField(allow_null=True, format="hex"))
    def get_delegate_uuid(self, obj):
        delegate = self._shown_delegate(obj)
        return delegate.uuid.hex if delegate else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_delegate_full_name(self, obj):
        delegate = self._shown_delegate(obj)
        return delegate.full_name if delegate else None

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_delegated_at(self, obj):
        return obj.delegated_at if self._shown_delegate(obj) else None

    @extend_schema_field(serializers.BooleanField())
    def get_can_delegate(self, obj):
        access = self._access()
        return bool(access) and contributions.may_delegate(access.user, obj, access)

    @extend_schema_field(ContributionDelegationCandidateSerializer(many=True))
    def get_delegation_candidates(self, obj):
        if not self.get_can_delegate(obj):
            return []
        return ContributionDelegationCandidateSerializer(
            contributions.delegation_candidates(obj), many=True
        ).data

    @extend_schema_field(serializers.BooleanField())
    def get_can_accept(self, obj):
        access = self._access()
        return bool(access) and contributions.may_accept(access.user, obj, access)

    @extend_schema_field(serializers.CharField())
    def get_coi_declaration_text(self, obj):
        if self.get_can_accept(obj):
            return coi_declarations.for_contribution(obj)
        return obj.coi_declaration_text

    @extend_schema_field(serializers.BooleanField())
    def get_can_fill(self, obj):
        access = self._access()
        return bool(access) and access.can_fill(obj)

    def _unanswered(self, obj):
        return contributions.unanswered_required_count(obj)

    @extend_schema_field(serializers.BooleanField())
    def get_checklist_completed(self, obj):
        return self._unanswered(obj) == 0

    @extend_schema_field(serializers.IntegerField())
    def get_unanswered_required_count(self, obj):
        return self._unanswered(obj)


class StepContributionWithAnswersSerializer(StepContributionSerializer):
    """A contribution with its form answers, for the step's threaded responses.

    ``user_uuid`` / ``user_full_name`` / ``user_image`` name the contributor,
    as the answers grouped per user did before steps had contributions.
    """

    user_uuid = serializers.SerializerMethodField()
    user_full_name = serializers.SerializerMethodField()
    user_image = serializers.SerializerMethodField()
    answers = serializers.SerializerMethodField()

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_user_uuid(self, obj):
        return self.get_contributor_uuid(obj)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_full_name(self, obj):
        name = self.get_contributor_full_name(obj)
        if name is None and obj.is_per_offering:
            return obj.requested_offering.offering.name
        return name

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_image(self, obj):
        if (
            not obj.contributor_id
            or self._anonymize(obj)
            or self._conceals_applicant(obj, "full_name")
        ):
            return None
        image = getattr(obj.contributor, "image", None)
        if not image:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(image.url) if request else image.url

    @extend_schema_field(TechnicalAssessmentAnswerSerializer(many=True))
    def get_answers(self, obj):
        completion = obj.get_checklist_completion(obj.proposal_step.call_step.checklist)
        if completion is None:
            return []
        answers = sorted(
            contributions.latest_answers(completion).values(),
            key=lambda answer: (answer.question.order, answer.question_id),
        )
        return TechnicalAssessmentAnswerSerializer(answers, many=True).data


class SubmitStepContributionSerializer(serializers.Serializer):
    contribution_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text=(
            "The contribution to submit. The caller's own contribution to "
            "the step when left out."
        ),
    )
    step_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text=(
            "UUID of the call step, naming the step whose contribution is "
            "meant. The proposal's current step when left out."
        ),
    )
    outcome = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "What the contribution concludes. On a per-offering step one of "
            "accepted, conditionally_accepted or rejected; elsewhere, if "
            "given, one of the step's outcomes."
        ),
    )
    conditions = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Conditions of a conditional acceptance.",
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class DeclineStepContributionSerializer(serializers.Serializer):
    contribution_uuid = serializers.UUIDField()
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class DelegateStepContributionSerializer(serializers.Serializer):
    contribution_uuid = serializers.UUIDField()
    user_uuid = serializers.UUIDField(
        allow_null=True,
        help_text=(
            "Who to hand the slot to, as the step's delegable_to allows; null "
            "takes it back."
        ),
    )


class AcceptStepContributionSerializer(serializers.Serializer):
    contribution_uuid = serializers.UUIDField()
    confirm_no_conflict = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Confirms having no conflict of interest with the proposal; "
            "required on a step that asks its evaluators to."
        ),
    )


class SubmitStepContributionResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    contribution = StepContributionSerializer()
    step_completed = serializers.BooleanField()
    proposal_state = serializers.CharField()


class OfferingAssessmentSerializer(serializers.Serializer):
    """What a per-offering assessment concluded about one requested offering."""

    contribution_uuid = serializers.UUIDField(source="uuid", format="hex")
    call_step_uuid = serializers.UUIDField(
        source="proposal_step.call_step.uuid", format="hex"
    )
    step_name = serializers.CharField(source="proposal_step.call_step.name")
    requested_offering_uuid = serializers.UUIDField(
        source="requested_offering.uuid", format="hex"
    )
    offering_uuid = serializers.UUIDField(
        source="requested_offering.offering.uuid", format="hex"
    )
    offering_name = serializers.CharField(source="requested_offering.offering.name")
    status = serializers.ChoiceField(choices=StepContributionStatuses.CHOICES)
    outcome = serializers.ChoiceField(
        choices=ContributionOutcomes.CHOICES, allow_blank=True
    )
    conditions = serializers.CharField(allow_blank=True)
    comment = serializers.CharField(allow_blank=True)
    submitted_at = serializers.DateTimeField(allow_null=True)


# Response serializer is now handled generically by ChecklistResponseSerializer
# Keep this for backward compatibility in call manager views
ProposalComplianceChecklistResponseSerializer = (
    checklist_serializers.ChecklistResponseSerializer
)


class CallComplianceChecklistInfoSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField()
    total_questions = serializers.IntegerField()
    required_questions = serializers.IntegerField()


class CallComplianceOverviewProposalReviewTriggerSerializer(serializers.Serializer):
    question = serializers.CharField()
    answer = serializers.JSONField()
    trigger_value = serializers.JSONField()
    operator = serializers.CharField()


class CallComplianceOverviewProposalComplianceSerializer(serializers.Serializer):
    is_completed = serializers.BooleanField()
    requires_review = serializers.BooleanField()
    completion_percentage = serializers.IntegerField()
    reviewed_by = serializers.CharField(allow_null=True)
    reviewed_at = serializers.DateTimeField(allow_null=True)
    review_triggers = CallComplianceOverviewProposalReviewTriggerSerializer(many=True)
    unanswered_required_count = serializers.IntegerField()


class CallComplianceOverviewProposalSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    state = serializers.CharField()
    created_by = serializers.CharField(allow_null=True)
    created_by_uuid = serializers.UUIDField(allow_null=True)
    compliance = CallComplianceOverviewProposalComplianceSerializer(allow_null=True)


class CallComplianceOverviewSerializer(serializers.Serializer):
    """Serializer for call manager compliance overview."""

    checklist = serializers.SerializerMethodField()
    proposals = serializers.SerializerMethodField()

    @extend_schema_field(CallComplianceChecklistInfoSerializer(allow_null=True))
    def get_checklist(self, call):
        """Get checklist information."""
        if not call.compliance_checklist:
            return None

        return {
            "uuid": str(call.compliance_checklist.uuid),
            "name": call.compliance_checklist.name,
            "description": call.compliance_checklist.description,
            "total_questions": call.compliance_checklist.questions.count(),
            "required_questions": call.compliance_checklist.questions.filter(
                required=True
            ).count(),
        }

    @extend_schema_field(CallComplianceOverviewProposalSerializer(many=True))
    def get_proposals(self, call):
        """Get proposal compliance status."""
        proposals_data = []

        for proposal in models.Proposal.objects.filter(round__call=call):
            proposal_data = {
                "uuid": str(proposal.uuid),
                "name": proposal.name,
                "state": proposal.state,
                "created_by": proposal.created_by.full_name
                if proposal.created_by
                else None,
                "created_by_uuid": str(proposal.created_by.uuid)
                if proposal.created_by
                else None,
                "compliance": None,
            }

            # Add compliance information if exists
            if hasattr(proposal, "checklist_completion"):
                completion = proposal.checklist_completion
                if completion:
                    proposal_data["compliance"] = {
                        "is_completed": completion.is_completed,
                        "requires_review": completion.requires_review,
                        "completion_percentage": completion.get_completion_percentage(),
                        "reviewed_by": completion.reviewed_by.full_name
                        if completion.reviewed_by
                        else None,
                        "reviewed_at": completion.reviewed_at,
                        "review_triggers": completion.get_review_trigger_summary(),
                        "unanswered_required_count": completion.get_unanswered_required_questions().count(),
                    }

            proposals_data.append(proposal_data)

        return proposals_data


class AvailableChecklistSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    checklist_type = serializers.CharField(read_only=True)
    questions_count = serializers.SerializerMethodField()
    category_name = serializers.CharField(read_only=True, allow_null=True)
    category_uuid = serializers.UUIDField(read_only=True, allow_null=True)

    @extend_schema_field(serializers.IntegerField())
    def get_questions_count(self, obj):
        return obj.questions.count()


class CallComplianceReviewSerializer(serializers.Serializer):
    """Serializer for call manager to review proposal compliance."""

    proposal_uuid = serializers.UUIDField()
    review_notes = serializers.CharField(required=False, allow_blank=True)

    def validate_proposal_uuid(self, value):
        """Validate that proposal belongs to the call."""
        call = self.context.get("call")
        if not call:
            raise serializers.ValidationError("Call context is required")

        try:
            proposal: models.Proposal = models.Proposal.objects.get(
                round__call=call, uuid=value
            )
            if not hasattr(proposal, "checklist_completion"):
                raise serializers.ValidationError(
                    "Proposal has no compliance checklist"
                )
            return value
        except models.Proposal.DoesNotExist:
            raise serializers.ValidationError("Proposal not found in this call")


# =============================================================================
# Reviewer Profile Serializers
# =============================================================================


class ReviewerStatsSerializer(serializers.Serializer):
    """A reviewer's record across every call, worked out from their reviews
    and assignments when read."""

    total_reviews_completed = serializers.IntegerField(
        read_only=True, help_text=_("Reviews the reviewer submitted.")
    )
    total_reviews_declined = serializers.IntegerField(
        read_only=True,
        help_text=_("Assignments and reviews the reviewer declined."),
    )
    total_reviews_timeout = serializers.IntegerField(
        read_only=True,
        help_text=_(
            "Assignments left unanswered until they expired, and reviews "
            "rejected at their deadline."
        ),
    )
    average_review_time_days = serializers.FloatField(
        read_only=True,
        allow_null=True,
        help_text=_("Mean days from a review's creation to its submission."),
    )
    average_score_given = serializers.FloatField(
        read_only=True,
        allow_null=True,
        help_text=_("Mean overall score of the submitted reviews, 1-5."),
    )
    last_review_date = serializers.DateField(
        read_only=True,
        allow_null=True,
        help_text=_("When the reviewer last submitted a review."),
    )


class ReviewerAffiliationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for reviewer affiliations."""

    organization_name_display = serializers.SerializerMethodField()
    organization = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Customer.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.ReviewerAffiliation
        fields = [
            "uuid",
            "organization",
            "organization_name",
            "organization_name_display",
            "organization_identifier",
            "department",
            "position_title",
            "start_date",
            "end_date",
            "is_primary",
            "affiliation_type",
            "created",
        ]

    def get_organization_name_display(self, obj) -> str:
        """Return organization name from linked Customer or from the text field."""
        if obj.organization:
            return obj.organization.name
        return obj.organization_name

    def validate(self, attrs):
        """Ensure at least organization or organization_name is provided."""
        organization = attrs.get("organization")
        organization_name = attrs.get("organization_name")

        if not organization and not organization_name:
            raise serializers.ValidationError(
                _("Either organization or organization_name must be provided.")
            )

        # If organization is linked, copy name for consistency
        if organization and not organization_name:
            attrs["organization_name"] = organization.name

        return attrs


class ExpertiseCategorySerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for expertise categories (taxonomy)."""

    class Meta:
        model = models.ExpertiseCategory
        fields = [
            "url",
            "uuid",
            "name",
            "code",
            "description",
            "parent",
            "level",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "parent": {
                "lookup_field": "uuid",
                "view_name": "expertise-category-detail",
                "required": False,
                "allow_null": True,
            },
        }


class ReviewerExpertiseSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for reviewer expertise keywords."""

    expertise_category_name = serializers.ReadOnlyField(
        source="expertise_category.name"
    )
    expertise_category = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ExpertiseCategory.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.ReviewerExpertise
        fields = [
            "uuid",
            "expertise_keyword",
            "expertise_category",
            "expertise_category_name",
            "proficiency_level",
            "years_experience",
            "last_active_date",
            "created",
        ]


class ReviewerPublicationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for reviewer publications."""

    # Declared explicitly so the schema renders an array; a bare JSONField is
    # mapped to a free-form object by JSONFieldExtension. The child is left
    # unconstrained because entries are {"name": ..., "orcid": ...} objects,
    # and plain name strings are still accepted for legacy records.
    coauthors = serializers.ListField(
        required=False,
        help_text=_("List of co-author names and identifiers"),
    )

    class Meta:
        model = models.ReviewerPublication
        fields = [
            "uuid",
            "title",
            "doi",
            "publication_year",
            "venue",
            "venue_type",
            "abstract",
            "coauthors",
            "external_ids",
            "is_excluded_from_matching",
            "created",
        ]


class ReviewerPublicationApprovalSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()

    class Meta:
        model = models.ReviewerPublicationRequest
        fields = ["uuid", "state", "created", "reviewed_at", "review_comment"]

    def get_state(self, obj) -> str:
        return dict(CoreReviewStates.CHOICES)[obj.state]


class ReviewerRegistryPoolSerializer(serializers.Serializer):
    call_uuid = serializers.UUIDField(source="call.uuid")
    call_name = serializers.CharField(source="call.name")


class ReviewerRegistrySerializer(serializers.ModelSerializer):
    """A reviewer as the platform operator sees them: standing, publication,
    the calls they review for and the review work they hold."""

    user_uuid = serializers.UUIDField(source="user.uuid", read_only=True)
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_email = serializers.ReadOnlyField(source="user.email")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_is_active = serializers.ReadOnlyField(source="user.is_active")
    standing_changed_by_name = serializers.ReadOnlyField(
        source="standing_changed_by.full_name", default=None
    )
    publication_request = serializers.SerializerMethodField()
    pools = serializers.SerializerMethodField(
        help_text=_("The calls whose reviewer pool the reviewer accepted.")
    )
    open_assignments = serializers.IntegerField(
        read_only=True,
        help_text=_(
            "Assignments still occupying the reviewer: pending offers and "
            "accepted ones whose review is not finished."
        ),
    )
    reviews_in_progress = serializers.IntegerField(
        read_only=True,
        help_text=_("Reviews in progress given without an assignment."),
    )

    class Meta:
        model = models.ReviewerProfile
        fields = [
            "uuid",
            "user_uuid",
            "user_full_name",
            "user_email",
            "user_username",
            "user_is_active",
            "standing",
            "standing_reason",
            "standing_changed_by_name",
            "standing_changed_at",
            "is_published",
            "published_at",
            "publication_request",
            "available_for_reviews",
            "pools",
            "open_assignments",
            "reviews_in_progress",
            "created",
        ]
        read_only_fields = fields

    @extend_schema_field(ReviewerPublicationApprovalSerializer(allow_null=True))
    def get_publication_request(self, obj):
        latest = next(
            (
                request
                for request in obj.publication_requests.all()
                if request.state != CoreReviewStates.CANCELED
            ),
            None,
        )
        return ReviewerPublicationApprovalSerializer(latest).data if latest else None

    @extend_schema_field(ReviewerRegistryPoolSerializer(many=True))
    def get_pools(self, obj):
        return ReviewerRegistryPoolSerializer(
            getattr(obj, "accepted_pools", []), many=True
        ).data


class ReviewerStandingSerializer(serializers.Serializer):
    standing = serializers.ChoiceField(choices=ReviewerStandings.CHOICES)
    reason = serializers.CharField(required=False, allow_blank=True)


class ReviewerProfileSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Full serializer for reviewer profiles with nested relationships."""

    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_email = serializers.ReadOnlyField(source="user.email")
    user_uuid = serializers.UUIDField(source="user.uuid", read_only=True)
    affiliations = ReviewerAffiliationSerializer(many=True, read_only=True)
    expertise_set = ReviewerExpertiseSerializer(many=True, read_only=True)
    # Declared explicitly so the schema renders an array; a bare JSONField is
    # mapped to a free-form object by JSONFieldExtension.
    alternative_names = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_("List of name variants used in publications"),
    )
    publications = ReviewerPublicationSerializer(many=True, read_only=True)
    stats = serializers.SerializerMethodField()
    orcid_connected = serializers.SerializerMethodField()
    profile_completeness = serializers.SerializerMethodField()
    publication_request = serializers.SerializerMethodField(
        help_text=_(
            "The latest request to publish the profile, while the operator "
            "approves publication: pending, or how it was decided and why."
        )
    )

    class Meta:
        model = models.ReviewerProfile
        fields = [
            "url",
            "uuid",
            "user",
            "user_uuid",
            "user_full_name",
            "user_email",
            "orcid_id",
            "orcid_connected",
            "orcid_last_sync",
            "biography",
            "alternative_names",
            "affiliations",
            "expertise_set",
            "publications",
            "stats",
            "profile_completeness",
            "is_published",
            "published_at",
            "publication_request",
            "available_for_reviews",
            "standing",
            "created",
            "modified",
        ]
        read_only_fields = [
            "user",
            "orcid_last_sync",
            "is_published",
            "published_at",
            "standing",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "user": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    @extend_schema_field(ReviewerPublicationApprovalSerializer(allow_null=True))
    def get_publication_request(self, obj):
        latest = obj.publication_requests.exclude(
            state=CoreReviewStates.CANCELED
        ).first()
        if latest is None:
            return None
        return ReviewerPublicationApprovalSerializer(latest).data

    @extend_schema_field(ReviewerStatsSerializer)
    def get_stats(self, obj):
        return ReviewerStatsSerializer(reviewer_stats.of_profile(obj)).data

    def get_orcid_connected(self, obj) -> bool:
        """Check if ORCID is connected (has access token)."""
        return bool(obj.orcid_access_token)

    def get_profile_completeness(self, obj) -> dict:
        """Calculate profile completeness percentage and missing fields."""
        checks = {
            "has_biography": bool(obj.biography),
            "has_orcid": bool(obj.orcid_id),
            "has_affiliations": obj.affiliations.exists(),
            "has_expertise": obj.expertise_set.exists(),
            "has_publications": obj.publications.exists(),
        }
        completed = sum(checks.values())
        total = len(checks)
        return {
            "percentage": round(completed / total * 100) if total else 0,
            "completed_checks": completed,
            "total_checks": total,
            "missing": [k for k, v in checks.items() if not v],
        }


class ReviewerProfileCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating a reviewer profile."""

    # Declared explicitly so the schema renders an array; a bare JSONField is
    # mapped to a free-form object by JSONFieldExtension.
    alternative_names = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_("List of name variants used in publications"),
    )

    class Meta:
        model = models.ReviewerProfile
        fields = [
            "orcid_id",
            "biography",
            "alternative_names",
            "available_for_reviews",
        ]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


# =============================================================================
# COI (Conflict of Interest) Serializers
# =============================================================================


class CallCOIConfigurationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for per-call COI configuration."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    # Explicitly type JSON array fields for proper OpenAPI schema generation.
    recusal_required_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        required=False,
        help_text="COI types requiring automatic recusal",
    )
    management_allowed_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        required=False,
        help_text="COI types allowing management plan",
    )
    disclosure_only_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        required=False,
        help_text="COI types requiring disclosure only",
    )

    class Meta:
        model = models.CallCOIConfiguration
        fields = [
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "coauthorship_lookback_years",
            "coauthorship_threshold_papers",
            "institutional_lookback_years",
            "include_same_department",
            "include_same_institution",
            "recusal_required_types",
            "management_allowed_types",
            "disclosure_only_types",
            "auto_detect_coauthorship",
            "auto_detect_institutional",
            "auto_detect_named_personnel",
            "invitation_proposal_disclosure",
            "created",
            "modified",
        ]
        read_only_fields = ["call"]
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Combine incoming values with the persisted ones so partial (PATCH)
        # updates are validated against the effective post-save state.
        def effective(field):
            if field in attrs:
                return attrs[field] or []
            if self.instance is not None:
                return getattr(self.instance, field) or []
            return []

        overlaps = models.CallCOIConfiguration.find_rule_overlaps(
            {
                field: effective(field)
                for field in models.CallCOIConfiguration.RULE_FIELDS
            }
        )

        # Block every overlap this request would create, but leave configurations
        # that already overlapped alone: they predate the rule, and rewriting or
        # freezing them is out of scope. An overlap can only be introduced by
        # rewriting one of the rules holding it, so this still makes new ones
        # impossible while an untouched legacy one stays editable.
        # Keyed on the value actually changing, not on the field being present.
        def is_rewritten(field):
            if field not in attrs:
                return False
            if self.instance is None:
                return True
            return set(attrs[field] or []) != set(getattr(self.instance, field) or [])

        introduced = {
            coi_type: fields
            for coi_type, fields in overlaps.items()
            if any(is_rewritten(field) for field in fields)
        }
        if introduced:
            listed = "; ".join(
                "%s (%s)" % (coi_type, ", ".join(sorted(fields)))
                for coi_type, fields in sorted(introduced.items())
            )
            raise serializers.ValidationError(
                _(
                    "Each conflict type may only be assigned to one rule. "
                    "Remove it from all but one of these: %(conflicts)s."
                )
                % {"conflicts": listed}
            )
        return attrs


class ConflictOfInterestSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for conflict of interest records."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    round_name = serializers.ReadOnlyField(source="proposal.round.name")
    round_uuid = serializers.UUIDField(source="proposal.round.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    reviewed_by_name = serializers.ReadOnlyField(source="reviewed_by.full_name")
    conflicting_user_name = serializers.ReadOnlyField(
        source="conflicting_user.full_name"
    )
    conflicting_organization_name = serializers.ReadOnlyField(
        source="conflicting_organization.name"
    )
    coi_type_display = serializers.CharField(
        source="get_coi_type_display", read_only=True
    )
    severity_display = serializers.CharField(
        source="get_severity_display", read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = models.ConflictOfInterest
        fields = [
            "url",
            "uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "proposal_slug",
            "round_uuid",
            "round_name",
            "call",
            "call_uuid",
            "call_name",
            "coi_type",
            "coi_type_display",
            "severity",
            "severity_display",
            "detection_method",
            "detected_at",
            "evidence_description",
            "evidence_data",
            "status",
            "status_display",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "management_plan",
            "conflicting_user",
            "conflicting_user_name",
            "conflicting_organization",
            "conflicting_organization_name",
            "created",
        ]
        read_only_fields = [
            "reviewer",
            "proposal",
            "call",
            "coi_type",
            "severity",
            "detection_method",
            "detected_at",
            "evidence_description",
            "evidence_data",
            # Only dismiss/waive/recuse may move the status: they also stamp
            # reviewed_by/reviewed_at and unblock the held assignment items.
            "status",
            "reviewed_by",
            "reviewed_at",
            "conflicting_user",
            "conflicting_organization",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewed_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "conflicting_user": {"lookup_field": "uuid", "view_name": "user-detail"},
            "conflicting_organization": {
                "lookup_field": "uuid",
                "view_name": "customer-detail",
            },
        }


class COIStatusUpdateSerializer(serializers.Serializer):
    """Serializer for updating COI status (dismiss/waive/recuse)."""

    status = serializers.ChoiceField(
        choices=[
            ("dismissed", "Dismiss - not a conflict"),
            ("waived", "Waive with management plan"),
            ("recused", "Recuse reviewer"),
        ]
    )
    review_notes = serializers.CharField(required=False, allow_blank=True)
    management_plan = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Required when status is 'waived'",
    )

    def validate(self, attrs):
        status = attrs.get("status")
        management_plan = attrs.get("management_plan")

        if status == "waived" and not management_plan:
            raise serializers.ValidationError(
                {
                    "management_plan": _(
                        "Management plan is required when waiving a conflict."
                    )
                }
            )

        return attrs


class ForceUnblockSerializer(serializers.Serializer):
    """Serializer for force-unblocking a COI-blocked assignment item."""

    override_reason = serializers.CharField(required=True)


class ForceAcceptPoolSerializer(serializers.Serializer):
    """Serializer for force-accepting a reviewer pool invitation."""

    override_reason = serializers.CharField(required=True)


class COIDisclosureFinancialInterestSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for financial interest disclosures."""

    class Meta:
        model = models.COIDisclosureFinancialInterest
        fields = [
            "uuid",
            "entity_name",
            "entity_type",
            "relationship_type",
            "amount_range",
            "is_ongoing",
            "description",
        ]


class COIDisclosureFormSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for COI disclosure forms."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    financial_interests = COIDisclosureFinancialInterestSerializer(
        many=True, read_only=True
    )

    class Meta:
        model = models.COIDisclosureForm
        fields = [
            "url",
            "uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "call",
            "call_uuid",
            "call_name",
            "certified",
            "certification_date",
            "certification_statement",
            "has_financial_interests",
            "financial_interests",
            "has_personal_relationships",
            "personal_relationships",
            "has_other_conflicts",
            "other_conflicts_description",
            "valid_until",
            "is_current",
            "created",
        ]
        read_only_fields = [
            "reviewer",
            "certification_date",
            "is_current",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "coi-disclosure-detail"},
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
                "required": False,
                "allow_null": True,
            },
        }


class COIPersonalRelationshipSerializer(serializers.Serializer):
    name = serializers.CharField()
    relationship_type = serializers.CharField()
    organization = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)


class COIDisclosureSubmitSerializer(serializers.Serializer):
    """Serializer for submitting a COI disclosure."""

    certification_statement = serializers.CharField()
    has_financial_interests = serializers.BooleanField(default=False)
    financial_interests = COIDisclosureFinancialInterestSerializer(
        many=True, required=False, default=list
    )
    has_personal_relationships = serializers.BooleanField(default=False)
    personal_relationships = COIPersonalRelationshipSerializer(
        many=True, required=False, default=list
    )
    has_other_conflicts = serializers.BooleanField(default=False)
    other_conflicts_description = serializers.CharField(
        required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        if attrs.get("has_financial_interests") and not attrs.get(
            "financial_interests"
        ):
            raise serializers.ValidationError(
                {
                    "financial_interests": _(
                        "Financial interests are required when indicated."
                    )
                }
            )
        if attrs.get("has_personal_relationships") and not attrs.get(
            "personal_relationships"
        ):
            raise serializers.ValidationError(
                {
                    "personal_relationships": _(
                        "Personal relationships must be specified."
                    )
                }
            )
        if attrs.get("has_other_conflicts") and not attrs.get(
            "other_conflicts_description"
        ):
            raise serializers.ValidationError(
                {
                    "other_conflicts_description": _(
                        "Other conflicts description is required."
                    )
                }
            )
        return attrs


class SelfDeclaredConflictSerializer(serializers.Serializer):
    """Serializer for reviewer self-declaring conflicts with specific proposals."""

    proposal_uuid = serializers.UUIDField()
    coi_type = serializers.ChoiceField(choices=COITypes.CHOICES)
    severity = serializers.ChoiceField(
        choices=COISeverityLevels.CHOICES,
        required=False,
        help_text="If omitted, the call's COI type handling decides it "
        "(recusal: real, management plan: apparent, disclosure only: "
        "potential), else the type's default severity.",
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_proposal_uuid(self, value):
        call = self.context.get("call")
        if not call:
            raise serializers.ValidationError(_("Call context is required."))
        try:
            proposal = models.Proposal.objects.get(uuid=value, round__call=call)
            return proposal
        except models.Proposal.DoesNotExist:
            raise serializers.ValidationError(_("Proposal not found in this call."))


# =============================================================================
# Reviewer Pool Serializers
# =============================================================================


class CallReviewerPoolSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for call reviewer pool membership.

    N+1 Optimization:
    When serializing multiple pool members, pass prefetched data via context:
    - coi_counts: dict mapping (reviewer_id, call_id) -> count
    - coi_by_severity: dict mapping (reviewer_id, call_id) -> {severity: count}
    - review_counts: dict mapping (user_id, call_id) -> {state: count}

    Or use annotations in the queryset:
    - annotated_coi_count
    - annotated_reviews_pending
    - annotated_reviews_in_progress
    - annotated_reviews_completed
    - open_assignments (``CallReviewerPool.objects.with_open_assignments()``)
    """

    reviewer_name = serializers.SerializerMethodField()
    reviewer_email = serializers.SerializerMethodField()
    reviewer_uuid = serializers.SerializerMethodField()
    reviewer_user_uuid = serializers.SerializerMethodField(
        help_text=(
            "The reviewer's user: the profile's, else the invited user's. "
            "Reviews name the reviewer by it."
        )
    )
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    invited_by_name = serializers.ReadOnlyField(source="invited_by.full_name")
    invited_user_name = serializers.ReadOnlyField(source="invited_user.full_name")
    # The deadline is applied here rather than waiting for the expiry task,
    # so an invitation that can no longer be answered never reads as pending.
    invitation_status = serializers.ChoiceField(
        source="effective_invitation_status",
        choices=ReviewerPoolInvitationStatuses.CHOICES,
        read_only=True,
    )
    invitation_status_display = serializers.CharField(
        source="get_effective_invitation_status_display", read_only=True
    )
    is_expired = serializers.BooleanField(
        source="is_invitation_expired",
        read_only=True,
        help_text="Whether the invitation can no longer be answered because "
        "its deadline has passed.",
    )
    has_profile = serializers.SerializerMethodField()
    coi_count = serializers.SerializerMethodField()
    coi_by_severity = serializers.SerializerMethodField()
    reviews_pending = serializers.SerializerMethodField()
    reviews_in_progress = serializers.SerializerMethodField()
    reviews_completed = serializers.SerializerMethodField()
    current_assignments = serializers.IntegerField(
        source="get_open_assignments",
        read_only=True,
        help_text="Number of the reviewer's open assignments in this call: "
        "pending or accepted assignment items whose review is not finished, "
        "plus reviews in progress created without an assignment item.",
    )
    overridden_by_name = serializers.ReadOnlyField(
        source="overridden_by.full_name", default=""
    )
    invitation_link = serializers.SerializerMethodField()
    reviewer_standing = serializers.SerializerMethodField(
        help_text=(
            "The reviewer's standing with the platform operator; only an active "
            "reviewer takes on new review work. Empty without a profile."
        )
    )

    @extend_schema_field(
        serializers.ChoiceField(choices=ReviewerStandings.CHOICES, allow_null=True)
    )
    def get_reviewer_standing(self, obj) -> str | None:
        return obj.reviewer.standing if obj.reviewer else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_invitation_link(self, obj) -> str | None:
        """Return the frontend invitation link path for pending invitations.

        This is a frontend SPA route (not a backend API URL), so we cannot
        use Django's reverse(). The frontend route is defined in
        waldur-homeport/src/proposals/routes.ts as /reviewer-invitation/:token/.
        """
        if (
            obj.effective_invitation_status != ReviewerPoolInvitationStatuses.PENDING
            or not obj.invitation_token
        ):
            return None
        return f"/reviewer-invitation/{obj.invitation_token}/"

    def get_reviewer_name(self, obj) -> str | None:
        """Get reviewer name from profile or invited_user."""
        if obj.reviewer:
            return obj.reviewer.user.full_name
        if obj.invited_user:
            return obj.invited_user.full_name
        return None

    def get_reviewer_email(self, obj) -> str | None:
        """Get email from profile, invited_user, or invited_email."""
        if obj.reviewer:
            return obj.reviewer.user.email
        if obj.invited_user:
            return obj.invited_user.email
        return obj.invited_email

    def get_reviewer_uuid(self, obj) -> str | None:
        """Get reviewer profile UUID if available."""
        if obj.reviewer:
            return str(obj.reviewer.uuid)
        return None

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_reviewer_user_uuid(self, obj) -> str | None:
        user = obj.reviewer.user if obj.reviewer_id else obj.invited_user
        return user.uuid.hex if user else None

    def get_has_profile(self, obj) -> bool:
        """Check if reviewer has a profile."""
        return obj.reviewer is not None

    def get_coi_count(self, obj) -> int:
        """Count total COIs for this reviewer in this call."""
        if not obj.reviewer:
            return 0

        # Check for annotation first (most efficient)
        if hasattr(obj, "annotated_coi_count"):
            return obj.annotated_coi_count or 0

        # Check for prefetched data in context
        coi_counts = self.context.get("coi_counts")
        if coi_counts is not None:
            key = (obj.reviewer_id, obj.call_id)
            return coi_counts.get(key, 0)

        # Fallback to query (N+1 warning: avoid in list views)
        return models.ConflictOfInterest.objects.filter(
            reviewer=obj.reviewer,
            call=obj.call,
        ).count()

    def get_coi_by_severity(self, obj) -> dict:
        """Count COIs by severity level."""
        if not obj.reviewer:
            return {}

        # Check for prefetched data in context
        coi_by_severity = self.context.get("coi_by_severity")
        if coi_by_severity is not None:
            key = (obj.reviewer_id, obj.call_id)
            return coi_by_severity.get(key, {})

        # Fallback to query (N+1 warning: avoid in list views)
        from django.db.models import Count

        counts = (
            models.ConflictOfInterest.objects.filter(
                reviewer=obj.reviewer,
                call=obj.call,
            )
            .values("severity")
            .annotate(count=Count("id"))
        )
        return {item["severity"]: item["count"] for item in counts}

    def _get_reviewer_user(self, obj):
        """Get the user associated with this pool member."""
        if obj.reviewer:
            return obj.reviewer.user
        return obj.invited_user

    def _get_reviewer_user_id(self, obj):
        """Get user ID for review lookups."""
        if obj.reviewer:
            return obj.reviewer.user_id
        if obj.invited_user:
            return obj.invited_user_id
        return None

    def get_reviews_pending(self, obj) -> int:
        """Legacy field - always returns 0.

        Previously counted reviews in 'created' state, but that state
        has been removed. Reviews are now created directly in 'in_review' state.
        Kept for backwards compatibility with frontend.
        """
        return 0

    def get_reviews_in_progress(self, obj) -> int:
        """Count reviews in 'in_review' state."""
        # Check for annotation first
        if hasattr(obj, "annotated_reviews_in_progress"):
            return obj.annotated_reviews_in_progress or 0

        # Check for prefetched data in context
        review_counts = self.context.get("review_counts")
        if review_counts is not None:
            user_id = self._get_reviewer_user_id(obj)
            if user_id:
                key = (user_id, obj.call_id)
                counts = review_counts.get(key, {})
                return counts.get(models.Review.States.IN_REVIEW, 0)
            return 0

        # Fallback to query
        user = self._get_reviewer_user(obj)
        if not user:
            return 0
        return models.Review.objects.filter(
            reviewer=user,
            proposal__round__call=obj.call,
            state=models.Review.States.IN_REVIEW,
        ).count()

    def get_reviews_completed(self, obj) -> int:
        """Count reviews in 'submitted' state."""
        # Check for annotation first
        if hasattr(obj, "annotated_reviews_completed"):
            return obj.annotated_reviews_completed or 0

        # Check for prefetched data in context
        review_counts = self.context.get("review_counts")
        if review_counts is not None:
            user_id = self._get_reviewer_user_id(obj)
            if user_id:
                key = (user_id, obj.call_id)
                counts = review_counts.get(key, {})
                return counts.get(models.Review.States.SUBMITTED, 0)
            return 0

        # Fallback to query
        user = self._get_reviewer_user(obj)
        if not user:
            return 0
        return models.Review.objects.filter(
            reviewer=user,
            proposal__round__call=obj.call,
            state=models.Review.States.SUBMITTED,
        ).count()

    class Meta:
        model = models.CallReviewerPool
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "reviewer",
            "reviewer_uuid",
            "reviewer_user_uuid",
            "reviewer_name",
            "reviewer_email",
            "has_profile",
            "reviewer_standing",
            "invited_email",
            "invited_user",
            "invited_user_name",
            "invited_at",
            "invitation_status",
            "invitation_status_display",
            "response_date",
            "decline_reason",
            "max_assignments",
            "current_assignments",
            "expertise_match_score",
            "invited_by_name",
            "invitation_link",
            "invitation_expires_at",
            "is_expired",
            "created",
            "coi_count",
            "coi_by_severity",
            "reviews_pending",
            "reviews_in_progress",
            "reviews_completed",
            "override_reason",
            "overridden_by_name",
            "overridden_at",
        ]
        read_only_fields = [
            "call",
            "reviewer",
            "invited_email",
            "invited_user",
            "invited_at",
            "invitation_status",
            "response_date",
            "decline_reason",
            "current_assignments",
            "invitation_expires_at",
            "override_reason",
            "overridden_at",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "invited_user": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class CallReviewerPoolUpdateSerializer(serializers.Serializer):
    """Serializer for updating reviewer pool member settings."""

    max_assignments = serializers.IntegerField(
        min_value=1,
        max_value=50,
        help_text="Maximum number of proposals that can be assigned to this reviewer",
    )

    def update(self, instance, validated_data):
        instance.max_assignments = validated_data.get(
            "max_assignments", instance.max_assignments
        )
        instance.save(update_fields=["max_assignments"])
        return instance


# The message is relayed verbatim in an email sent on the platform's behalf,
# so keep it to a short note rather than an arbitrary payload.
INVITATION_MESSAGE_MAX_LENGTH = 2000


class ReviewerInvitationSerializer(serializers.Serializer):
    """Serializer for inviting reviewers to a call."""

    reviewer_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        help_text="List of reviewer profile UUIDs to invite",
    )
    max_assignments = serializers.IntegerField(default=5, min_value=1)
    invitation_message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=INVITATION_MESSAGE_MAX_LENGTH,
        help_text="Custom message to include in invitation email",
    )


class ReviewerInvitationResponseSerializer(serializers.Serializer):
    """Serializer for responding to a reviewer invitation (via token)."""

    accept = serializers.BooleanField()
    decline_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Reason for declining (required if accept=False)",
    )

    def validate(self, attrs):
        if not attrs.get("accept") and not attrs.get("decline_reason"):
            raise serializers.ValidationError(
                {
                    "decline_reason": _(
                        "Reason is required when declining an invitation."
                    )
                }
            )
        return attrs


class EmailInvitationSerializer(serializers.Serializer):
    """Serializer for inviting a reviewer by email address."""

    email = serializers.EmailField(help_text="Email address to send the invitation to")
    invitation_message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=INVITATION_MESSAGE_MAX_LENGTH,
        help_text="Custom message to include in invitation email",
    )
    max_assignments = serializers.IntegerField(default=5, min_value=1)


class ReviewerSuggestionTopMatchingProposalSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField(required=False)
    slug = serializers.CharField(required=False)
    affinity = serializers.FloatField()
    keyword_score = serializers.FloatField(required=False, allow_null=True)
    text_score = serializers.FloatField(required=False, allow_null=True)
    has_coi = serializers.BooleanField(required=False, allow_null=True)
    coi_type = serializers.CharField(required=False, allow_null=True)
    coi_severity = serializers.CharField(required=False, allow_null=True)


class ReviewerSuggestionSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for algorithm-generated reviewer suggestions."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_email = serializers.ReadOnlyField(source="reviewer.user.email")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    reviewer_biography = serializers.ReadOnlyField(source="reviewer.biography")
    call_name = serializers.ReadOnlyField(source="call.name")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    reviewed_by_name = serializers.ReadOnlyField(source="reviewed_by.full_name")
    pool_invitation_status = serializers.SerializerMethodField(
        help_text=(
            "Where the reviewer stands in the call's reviewer pool -- invited "
            "from this suggestion or directly: pending, accepted, declined or "
            "expired. Empty when not invited."
        )
    )

    source_type_display = serializers.CharField(
        source="get_source_type_display", read_only=True
    )
    matched_keywords = serializers.ListField(
        child=serializers.CharField(), read_only=True
    )
    top_matching_proposals = ReviewerSuggestionTopMatchingProposalSerializer(
        many=True, read_only=True
    )

    @extend_schema_field(
        serializers.ChoiceField(
            choices=ReviewerPoolInvitationStatuses.CHOICES, allow_null=True
        )
    )
    def get_pool_invitation_status(self, suggestion) -> str | None:
        # One query per call, not per suggestion.
        pools = self.context.setdefault("_pool_statuses", {})
        if suggestion.call_id not in pools:
            pools[suggestion.call_id] = dict(
                models.CallReviewerPool.objects.filter(
                    call_id=suggestion.call_id, reviewer__isnull=False
                ).values_list("reviewer_id", "invitation_status")
            )
        return pools[suggestion.call_id].get(suggestion.reviewer_id)

    class Meta:
        model = models.ReviewerSuggestion
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "reviewer_email",
            "reviewer_biography",
            "affinity_score",
            "keyword_score",
            "text_score",
            "status",
            "status_display",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "rejection_reason",
            "pool_invitation_status",
            "matched_keywords",
            "top_matching_proposals",
            "source_type",
            "source_type_display",
            "created",
        ]
        read_only_fields = [
            "call",
            "reviewer",
            "affinity_score",
            "keyword_score",
            "text_score",
            "reviewed_by",
            "reviewed_at",
            "matched_keywords",
            "top_matching_proposals",
            "source_type",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "reviewed_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class SuggestionRejectSerializer(serializers.Serializer):
    """Serializer for rejecting a reviewer suggestion."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Reason for rejecting the suggestion",
    )


class OrcidCallbackSerializer(serializers.Serializer):
    """Serializer for ORCID OAuth callback."""

    code = serializers.CharField(
        help_text="Authorization code from ORCID OAuth callback",
    )
    state = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="State token for CSRF protection",
    )


class ImportPublicationsSerializer(serializers.Serializer):
    """Serializer for importing publications from various sources."""

    source = serializers.ChoiceField(
        choices=[("orcid", "ORCID"), ("doi", "DOI")],
        default="orcid",
        help_text="Source to import publications from",
    )
    doi = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="DOI of publication to import (required if source is 'doi')",
    )

    def validate(self, attrs):
        if attrs.get("source") == "doi" and not attrs.get("doi"):
            raise serializers.ValidationError(
                {"doi": _("DOI is required when source is 'doi'.")}
            )
        return attrs


# =============================================================================
# Matching Serializers
# =============================================================================


class MatchingConfigurationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for matching algorithm configuration."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.MatchingConfiguration
        fields = [
            "uuid",
            "call_uuid",
            "call_name",
            "affinity_method",
            "keyword_weight",
            "text_weight",
            "min_reviewers_per_proposal",
            "max_reviewers_per_proposal",
            "min_proposals_per_reviewer",
            "max_proposals_per_reviewer",
            "algorithm",
            "min_affinity_threshold",
            "use_reviewer_bids",
            "bid_weight",
            "created",
            "modified",
        ]


class ReviewerProposalAffinitySerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for reviewer-proposal affinity scores."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)

    class Meta:
        model = models.ReviewerProposalAffinity
        fields = [
            "uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "affinity_score",
            "keyword_score",
            "text_score",
            "created",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
        }


class AffinityMatrixEntrySerializer(serializers.Serializer):
    """Serializer for a single entry in the affinity matrix."""

    uuid = serializers.UUIDField()
    reviewer_uuid = serializers.UUIDField()
    reviewer_name = serializers.CharField()
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    affinity_score = serializers.FloatField()
    keyword_score = serializers.FloatField(allow_null=True)
    text_score = serializers.FloatField(allow_null=True)
    # COI fields
    has_conflict = serializers.BooleanField()
    coi_type = serializers.CharField(allow_null=True)
    coi_severity = serializers.CharField(allow_null=True)
    coi_status = serializers.CharField(allow_null=True)
    # Source field: "pool" or "suggestion"
    source = serializers.CharField()


class AffinityMatrixResponseSerializer(serializers.Serializer):
    """Serializer for the affinity matrix response."""

    count = serializers.IntegerField()
    results = AffinityMatrixEntrySerializer(many=True)


# Response serializers for action endpoints


class MessageResponseSerializer(serializers.Serializer):
    """Generic message response serializer."""

    message = serializers.CharField()


class DuplicateCallRequestSerializer(serializers.Serializer):
    """Request body for the protected-calls duplicate action."""

    name = serializers.CharField(
        max_length=models.Call._meta.get_field("name").max_length,
        required=True,
        allow_blank=False,
        trim_whitespace=True,
    )
    # Section flags mirror marketplace offering import (`include_*`). Each
    # defaults to True; uncheck to skip that part of the source's configuration.
    copy_documents = serializers.BooleanField(required=False, default=True)
    copy_offerings = serializers.BooleanField(required=False, default=True)
    copy_rounds = serializers.BooleanField(required=False, default=True)
    copy_workflow_steps = serializers.BooleanField(required=False, default=True)
    copy_resource_templates = serializers.BooleanField(required=False, default=True)
    copy_budgets = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "Copy the resource pools. Needs copy_offerings, which copies the "
            "component rules they are set on; a round's pool is copied only "
            "with copy_rounds, onto the copied round."
        ),
    )
    copy_role_mappings = serializers.BooleanField(required=False, default=True)
    copy_applicant_visibility_config = serializers.BooleanField(
        required=False, default=True
    )
    copy_proposal_field_config = serializers.BooleanField(required=False, default=True)
    copy_coi_configuration = serializers.BooleanField(required=False, default=True)
    copy_matching_configuration = serializers.BooleanField(required=False, default=True)
    copy_assignment_configuration = serializers.BooleanField(
        required=False, default=True
    )


class CallExportParametersSerializer(serializers.Serializer):
    """Request body for the protected-calls export_call action.

    Call settings are always exported; each flag adds one section.
    """

    include_documents = serializers.BooleanField(required=False, default=True)
    include_rounds = serializers.BooleanField(required=False, default=True)
    include_offerings = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Requested offerings together with their resource templates.",
    )
    include_workflow_steps = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Workflow steps with their notification rules and criteria.",
    )
    include_field_configs = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Proposal field and applicant visibility configuration.",
    )
    include_review_configs = serializers.BooleanField(
        required=False,
        default=True,
        help_text="COI, reviewer matching and assignment configuration.",
    )
    include_role_mappings = serializers.BooleanField(required=False, default=True)
    include_compliance_checklist = serializers.BooleanField(
        required=False, default=True
    )

    def sections(self) -> dict[str, bool]:
        return {
            key.removeprefix("include_"): value
            for key, value in self.validated_data.items()
        }


class CallExportResponseSerializer(serializers.Serializer):
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    export_data = serializers.JSONField()
    exported_sections = serializers.ListField(child=serializers.CharField())
    export_timestamp = serializers.DateTimeField()
    warnings = serializers.ListField(
        child=serializers.CharField(),
        help_text="Parts that could not be exported, such as unreadable documents.",
    )


# An exported call is tens of kilobytes; anything near this is not one.
CALL_DATA_MAX_BYTES = 2 * 1024 * 1024
CALL_DATA_TOO_LARGE = (
    f"The call document is larger than {CALL_DATA_MAX_BYTES // (1024 * 1024)} MB."
)


class NoAliasSafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` refusing anchors and aliases.

    An export never writes them, and a few nested aliases expand into
    billions of nodes once the document is stored as JSON.
    """

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML anchors and aliases are not supported",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def parse_call_document(value):
    """A call document given as a mapping or a YAML string, as a mapping.

    YAML is read with the safe loader, refusing anchors and aliases, and
    either form is refused beyond CALL_DATA_MAX_BYTES.
    """
    if isinstance(value, str):
        if len(value.encode()) > CALL_DATA_MAX_BYTES:
            raise serializers.ValidationError(CALL_DATA_TOO_LARGE)
        try:
            value = yaml.load(value, Loader=NoAliasSafeLoader)  # noqa: S506
        except yaml.YAMLError as e:
            raise serializers.ValidationError(f"Invalid YAML data: {e}")
    elif len(json.dumps(value, default=str).encode()) > CALL_DATA_MAX_BYTES:
        raise serializers.ValidationError(CALL_DATA_TOO_LARGE)
    if not isinstance(value, dict):
        raise serializers.ValidationError("Expected a mapping.")
    return value


class CallImportParametersSerializer(serializers.Serializer):
    """Request body for the protected-calls import_call action.

    The call comes either from an uploaded document (``call_data``) or from
    a call template (``template``).
    """

    manager = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallManagingOrganisation.objects.all(),
        help_text="Call managing organization that will own the imported call.",
    )
    name = serializers.CharField(
        max_length=models.Call._meta.get_field("name").max_length,
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        help_text="Name for the imported call. Defaults to the exported name.",
    )
    call_data = serializers.JSONField(
        required=False,
        help_text=(
            "Exported call document, as a mapping or a YAML string. "
            "Give this or template."
        ),
    )
    template = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallTemplate.objects.filter(is_published=True),
        required=False,
        help_text=(
            "Published call template to create the call from. Give this or call_data."
        ),
    )
    import_documents = serializers.BooleanField(required=False, default=True)
    import_rounds = serializers.BooleanField(required=False, default=True)
    import_offerings = serializers.BooleanField(required=False, default=True)
    import_workflow_steps = serializers.BooleanField(required=False, default=True)
    import_field_configs = serializers.BooleanField(required=False, default=True)
    import_review_configs = serializers.BooleanField(required=False, default=True)
    import_role_mappings = serializers.BooleanField(required=False, default=True)
    import_compliance_checklist = serializers.BooleanField(required=False, default=True)

    def validate_manager(self, manager):
        # Raised from validation, not the view, so that the serializer cannot
        # be used to import a call without this check.
        if not proposal_permissions.may_create_call_in(
            self.context["request"], manager
        ):
            raise PermissionDenied()
        return manager

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        # Staff may create a call from an unpublished template, to try it out;
        # anyone else sees the published ones only.
        if request is not None and request.user.is_staff:
            fields["template"].queryset = models.CallTemplate.objects.all()
        return fields

    def validate_call_data(self, value):
        return parse_call_document(value)

    def validate(self, attrs):
        has_data = attrs.get("call_data") is not None
        template = attrs.get("template")
        if has_data == (template is not None):
            raise serializers.ValidationError(
                {"call_data": "Give either call_data or template, not both."}
                if has_data
                else {"call_data": "Give call_data or template."}
            )
        if template is not None:
            attrs["call_data"] = template.document
        return attrs

    def sections(self) -> dict[str, bool]:
        sections = {
            key.removeprefix("import_"): value
            for key, value in self.validated_data.items()
            if key.startswith("import_")
        }
        if self.validated_data.get("template") is not None:
            # A template carries settings and workflow only.
            sections.update(call_templates.EXCLUDED_SECTIONS)
        return sections


class CallImportResponseSerializer(serializers.Serializer):
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    imported_sections = serializers.ListField(child=serializers.CharField())
    warnings = serializers.ListField(child=serializers.CharField())


class ScenarioTotalsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    passed = serializers.IntegerField()
    failed = serializers.IntegerField()
    error = serializers.IntegerField()


class CallTemplateStepSerializer(serializers.Serializer):
    name = serializers.CharField()
    kind = serializers.CharField(allow_null=True)
    order = serializers.IntegerField(allow_null=True)
    is_enabled = serializers.BooleanField()
    phase = serializers.ChoiceField(choices=WorkflowPhases.CHOICES)


class CallTemplateProgrammeSerializer(serializers.Serializer):
    """A programme presets belong to: who runs it and where it is described."""

    key = serializers.CharField()
    name = serializers.CharField()
    organization = serializers.CharField(help_text="Who runs the programme.")
    organization_url = serializers.URLField(required=False, allow_null=True)
    reference_url = serializers.URLField(
        required=False,
        allow_null=True,
        help_text="Public page describing the programme.",
    )
    summary = serializers.CharField(required=False, allow_null=True)


class CallTemplatePresetInfoSerializer(serializers.Serializer):
    """The preset block of a template document: the process it models."""

    organization = serializers.CharField(
        allow_null=True,
        help_text=(
            "Who runs the process; the programme's organization for a process "
            "of a programme, null for a generic pattern naming none."
        ),
    )
    organization_url = serializers.URLField(required=False, allow_null=True)
    process = serializers.CharField(help_text="Name of the process modeled.")
    reference_url = serializers.URLField(
        required=False,
        allow_null=True,
        help_text="Public page describing the process.",
    )
    reference_title = serializers.CharField(required=False, allow_null=True)
    category = serializers.ChoiceField(
        choices=call_templates.PRESET_CATEGORIES,
        help_text=(
            "generic: a pattern any organization can adopt; programme: the "
            "process of one named programme."
        ),
    )
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    summary = serializers.CharField()
    programme = CallTemplateProgrammeSerializer(
        allow_null=True,
        help_text=(
            "The programme the process belongs to; null for a process of no "
            "programme, such as a generic one."
        ),
    )


class CallTemplateOriginSerializer(serializers.Serializer):
    """The public facts of the process a template models."""

    organization = serializers.CharField()
    process = serializers.CharField()
    reference_url = serializers.URLField(allow_null=True)
    programme = serializers.CharField(
        allow_null=True,
        help_text="Name of the programme the process belongs to, if any.",
    )


# What a call manager sees of a published template; the document itself, with
# the questions of its forms, and the bookkeeping are for staff.
CALL_TEMPLATE_PUBLIC_FIELDS = (
    "uuid",
    "name",
    "description",
    "schema_version",
    "steps",
    "origin",
    "modified",
)


class CallTemplateSerializer(serializers.ModelSerializer):
    document = serializers.JSONField(
        help_text=(
            "Call document in the export format, as a mapping or a YAML "
            "string: call settings and workflow, without rounds, requested "
            "offerings or documents."
        ),
    )
    steps = serializers.SerializerMethodField()
    preset = serializers.SerializerMethodField(
        help_text=(
            "The preset block of the template's own document: where the "
            "process it models comes from; null when it does not say."
        )
    )
    origin = serializers.SerializerMethodField(
        help_text=(
            "Organization, process and reference page of the process this "
            "template models; null when it does not say."
        )
    )
    created_by = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    warnings = serializers.SerializerMethodField(
        help_text=(
            "What a call created from the saved document would leave out, "
            "such as forms or roles this portal does not have."
        )
    )
    scenarios = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text=(
            "Named paths through the workflow: {key, name, description, "
            "request: {offerings, actions}, assertions}. Definitions "
            "only: they run as dry runs on each call created from the template."
        ),
    )

    class Meta:
        model = models.CallTemplate
        fields = (
            "uuid",
            "name",
            "description",
            "document",
            "schema_version",
            "is_published",
            "preset",
            "origin",
            "steps",
            "created_by",
            "created",
            "modified",
            "warnings",
            "scenarios",
        )
        read_only_fields = (
            "uuid",
            "schema_version",
            "created",
            "modified",
        )

    def get_fields(self):
        fields = super().get_fields()
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields
        request = self.context.get("request")
        if request is None or not request.user.is_staff:
            fields = {
                name: field
                for name, field in fields.items()
                if name in CALL_TEMPLATE_PUBLIC_FIELDS
            }
        return fields

    @extend_schema_field(CallTemplateStepSerializer(many=True))
    def get_steps(self, template):
        return call_templates.step_summary(template.document)

    def _preset_info(self, template) -> dict | None:
        info = (template.document or {}).get(call_templates.PRESET_KEY)
        if info is None:
            return None
        if "programmes" not in self.context:
            self.context["programmes"] = call_templates.programmes()
        try:
            # Naming a programme no longer shipped, or stored before blocks
            # were checked: only a well-formed one shows.
            return call_templates.resolve_preset(
                call_templates.check_preset(info), self.context["programmes"]
            )
        except serializers.ValidationError:
            return None

    @extend_schema_field(CallTemplatePresetInfoSerializer(allow_null=True))
    def get_preset(self, template):
        return self._preset_info(template)

    @extend_schema_field(CallTemplateOriginSerializer(allow_null=True))
    def get_origin(self, template):
        info = self._preset_info(template)
        if not info:
            return None
        programme = info.get("programme")
        return {
            "organization": info.get("organization") or "",
            "process": info.get("process") or "",
            "reference_url": info.get("reference_url"),
            "programme": programme["name"] if programme else None,
        }

    def get_warnings(self, template) -> list[str]:
        return getattr(self, "_warnings", [])

    def validate_document(self, value):
        return call_templates.check_document(parse_call_document(value))

    def validate_scenarios(self, value):
        return validate_template_scenarios(value)

    def validate(self, attrs):
        document = attrs.get("document")
        if document is not None:
            attrs["schema_version"] = document["schema_version"]
            try:
                self._warnings = call_templates.dry_run(
                    document, self.context["request"].user
                )
            except serializers.ValidationError as e:
                raise serializers.ValidationError({"document": e.detail})
        return attrs


class CallTemplatePresetSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    steps = CallTemplateStepSerializer(many=True)
    preset = CallTemplatePresetInfoSerializer(
        allow_null=True,
        help_text="Organization and process the preset models; null when it does not say.",
    )
    name_taken = serializers.BooleanField(
        help_text=(
            "A call template already holds the name this preset would be "
            "loaded under: rename or delete it to load the preset."
        )
    )


class LoadCallTemplatePresetSerializer(serializers.Serializer):
    presets = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
        help_text="Keys of the presets to load, as the presets action lists them.",
    )
    publish = serializers.BooleanField(
        default=False,
        help_text="Publish the templates created, so call managers see them.",
    )

    def validate_presets(self, value):
        unknown = sorted(set(value) - set(call_templates.names()))
        if unknown:
            raise serializers.ValidationError(
                _("Unknown presets: %s.") % ", ".join(unknown)
            )
        return value


class LoadCallTemplatePresetResponseSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    created_names = serializers.ListField(child=serializers.CharField())


class CallSaveAsTemplateSerializer(serializers.Serializer):
    """Request body for the protected-calls save_as_template action."""

    name = serializers.CharField(
        max_length=models.CallTemplate._meta.get_field("name").max_length,
        required=False,
        allow_blank=False,
        help_text="Name of the template. Defaults to the call's name.",
    )
    description = serializers.CharField(required=False, allow_blank=True)


class BulkRoundCreateRequestSerializer(serializers.ModelSerializer):
    """Request body for the rounds_bulk_set action.

    Combines a single round's configuration with cadence parameters so the
    server can spawn N evenly-spaced rounds in one shot. ``cutoff_time`` and
    ``allocation_date`` are intentionally excluded: cutoffs are derived from
    ``submission_window_days``; per-round fixed allocation dates don't make
    sense across a series and are explicitly disallowed in bulk mode.
    """

    cadence = serializers.ChoiceField(choices=BulkRoundCadence.CHOICES, required=True)
    custom_interval_months = serializers.IntegerField(
        min_value=1, required=False, allow_null=True
    )
    submission_window_days = serializers.IntegerField(min_value=1, required=True)
    number_of_rounds = serializers.IntegerField(
        min_value=1, max_value=60, required=True
    )

    class Meta:
        model = models.Round
        fields = [
            "start_time",
            "review_duration_in_days",
            "cadence",
            "custom_interval_months",
            "submission_window_days",
            "number_of_rounds",
        ]

    def validate(self, attrs):
        if attrs["cadence"] == BulkRoundCadence.CUSTOM and not attrs.get(
            "custom_interval_months"
        ):
            raise serializers.ValidationError(
                {"custom_interval_months": _("Required when cadence is set to custom.")}
            )
        return attrs


class ComputeAffinitiesResponseSerializer(serializers.Serializer):
    """Response for compute_affinities action."""

    computed_count = serializers.IntegerField()
    message = serializers.CharField()


class GenerateSuggestionsRequestSerializer(serializers.Serializer):
    """Request parameters for configurable suggestion generation."""

    source = serializers.ChoiceField(
        choices=[
            ("call_description", "Call Description"),
            ("all_proposals", "All Proposals"),
            ("selected_proposals", "Selected Proposals"),
            ("custom_keywords", "Custom Keywords"),
        ],
        default="all_proposals",
        help_text="What content to match reviewers against",
    )

    # For 'selected_proposals' source
    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        help_text="Specific proposal UUIDs to match against (for selected_proposals source)",
    )

    # For 'custom_keywords' source
    keywords = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
        help_text="Custom keywords to search for (for custom_keywords source)",
    )

    # Keyword search mode (for custom_keywords source)
    keyword_search_mode = serializers.ChoiceField(
        choices=[
            ("expertise_only", "Match against reviewer expertise keywords"),
            ("full_text", "Search all reviewer content"),
        ],
        default="expertise_only",
        required=False,
        help_text="How to search for custom keywords",
    )

    # Override default threshold
    min_affinity_threshold = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        help_text="Minimum affinity score for suggestions (0.0-1.0)",
    )

    def validate(self, attrs):
        source = attrs.get("source", "all_proposals")

        if source == "selected_proposals":
            if not attrs.get("proposal_uuids"):
                raise serializers.ValidationError(
                    {"proposal_uuids": "Required when source is 'selected_proposals'"}
                )

        if source == "custom_keywords":
            if not attrs.get("keywords"):
                raise serializers.ValidationError(
                    {"keywords": "Required when source is 'custom_keywords'"}
                )

        return attrs


class GenerateSuggestionsResponseSerializer(serializers.Serializer):
    """Response for generate_suggestions action."""

    suggestions_created = serializers.IntegerField()
    reviewers_evaluated = serializers.IntegerField()
    source_used = serializers.CharField()
    suggestions = serializers.ListField(child=serializers.CharField())


class SendInvitationsResponseSerializer(serializers.Serializer):
    """Response for send_invitations action."""

    invitations_sent = serializers.IntegerField()
    skipped = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=(
            "Confirmed suggestions left uninvited, and why: their reviewer is no "
            "longer in good standing, or outside the call's reviewer eligibility."
        ),
    )


class ConflictSummaryResponseSerializer(serializers.Serializer):
    """Response for conflict_summary action."""

    total = serializers.IntegerField()
    by_status = serializers.DictField(child=serializers.IntegerField())
    by_severity = serializers.DictField(child=serializers.IntegerField())
    by_type = serializers.DictField(child=serializers.IntegerField())


class OrcidSyncResponseSerializer(serializers.Serializer):
    """Response for sync_orcid action."""

    imported = serializers.DictField()
    last_sync = serializers.DateTimeField()


class OrcidDisconnectResponseSerializer(serializers.Serializer):
    """Response for disconnect_orcid action."""

    detail = serializers.CharField()


class ProposedAssignmentSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for proposed reviewer-proposal assignments."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    deployed_by_name = serializers.ReadOnlyField(source="deployed_by.full_name")

    class Meta:
        model = models.ProposedAssignment
        fields = [
            "url",
            "uuid",
            "call",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "affinity_score",
            "algorithm_used",
            "rank",
            "is_deployed",
            "deployed_at",
            "deployed_by",
            "deployed_by_name",
            "created",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "deployed_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class COIDetectionJobSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for COI detection job status."""

    url = serializers.SerializerMethodField()
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    progress_percentage = serializers.SerializerMethodField()

    class Meta:
        model = models.COIDetectionJob
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "job_type",
            "state",
            "total_pairs",
            "processed_pairs",
            "progress_percentage",
            "conflicts_found",
            "started_at",
            "completed_at",
            "error_message",
            "created",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }

    def get_url(self, obj) -> str:
        """Return URL for the job detail endpoint."""
        from rest_framework.reverse import reverse

        request = self.context.get("request")
        return reverse(
            "coi-detection-job-detail",
            kwargs={"uuid": obj.uuid.hex},
            request=request,
        )

    def get_progress_percentage(self, obj) -> float:
        if obj.total_pairs == 0:
            return 0.0
        return round(obj.processed_pairs / obj.total_pairs * 100, 1)


class TriggerCOIDetectionSerializer(serializers.Serializer):
    """Serializer for triggering COI detection."""

    job_type = serializers.ChoiceField(
        choices=[
            ("full_call", "Full call detection"),
            ("incremental", "Incremental detection"),
        ],
        default="full_call",
    )


class DeployAssignmentsSerializer(serializers.Serializer):
    """Serializer for deploying proposed assignments as actual reviews."""

    assignment_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Specific assignments to deploy. If empty, deploys all non-deployed assignments.",
    )


# =============================================================================
# Reviewer Bid Serializers
# =============================================================================


class ReviewerBidSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for reviewer bids."""

    reviewer_name = serializers.ReadOnlyField(source="reviewer.user.full_name")
    reviewer_uuid = serializers.UUIDField(source="reviewer.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    bid_display = serializers.CharField(source="get_bid_display", read_only=True)

    class Meta:
        model = models.ReviewerBid
        fields = [
            "uuid",
            "call",
            "call_uuid",
            "reviewer",
            "reviewer_uuid",
            "reviewer_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "bid",
            "bid_display",
            "comment",
            "submitted_at",
            "modified_at",
        ]
        # A bid is placed on a proposal through submit; an update changes only
        # the bid and its comment.
        read_only_fields = [
            "call",
            "reviewer",
            "proposal",
            "submitted_at",
            "modified_at",
        ]
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer": {
                "lookup_field": "uuid",
                "view_name": "reviewer-profile-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
        }


class ReviewerBidSubmitSerializer(serializers.Serializer):
    """Serializer for submitting a bid on a proposal."""

    proposal_uuid = serializers.UUIDField()
    bid = serializers.ChoiceField(
        choices=[
            ("eager", "Eager to review"),
            ("willing", "Willing to review"),
            ("not_willing", "Not willing to review"),
            ("conflict", "Has conflict of interest"),
        ]
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class ReviewerBulkBidSerializer(serializers.Serializer):
    """Serializer for submitting multiple bids at once."""

    bids = ReviewerBidSubmitSerializer(many=True)


# =============================================================================
# Public Invitation Serializers
# =============================================================================


class InvitationCOIConfigurationSerializer(serializers.Serializer):
    """COI configuration info for invitation display."""

    recusal_required_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        help_text="COI types requiring automatic recusal",
    )
    management_allowed_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        help_text="COI types where a management plan can be submitted",
    )
    disclosure_only_types = serializers.ListField(
        child=serializers.ChoiceField(choices=COITypes.CHOICES),
        help_text="COI types that only need disclosure",
    )
    proposal_disclosure_level = serializers.CharField(
        help_text="How much proposal info is disclosed to reviewers",
    )


class InvitationProposalSummarySerializer(serializers.Serializer):
    """Proposal summary for invitation display."""

    uuid = serializers.UUIDField()
    name = serializers.CharField()
    summary = serializers.CharField(required=False, allow_null=True)


class COITypeChoiceSerializer(serializers.Serializer):
    """COI type choice tuple (value, label)."""

    value = serializers.CharField()
    label = serializers.CharField()


class PublicInvitationSerializer(serializers.Serializer):
    """Serializer for public invitation information (no auth required)."""

    call_name = serializers.CharField(read_only=True)
    call_uuid = serializers.UUIDField(read_only=True)
    invitation_status = serializers.CharField(read_only=True)
    invited_at = serializers.DateTimeField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    is_expired = serializers.BooleanField(read_only=True)
    max_assignments = serializers.IntegerField(read_only=True, allow_null=True)
    invited_by_name = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Name of the person who sent the invitation",
    )
    profile_status = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="User's profile status: 'published', 'unpublished', 'missing', or null if not authenticated",
    )
    requires_profile = serializers.BooleanField(
        read_only=True,
        help_text="Whether the invitation requires creating a reviewer profile",
    )
    coi_configuration = InvitationCOIConfigurationSerializer(
        read_only=True,
        allow_null=True,
        help_text="COI configuration for this call",
    )
    coi_types = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField()),
        read_only=True,
        help_text="Available COI types as list of [value, label] tuples",
    )
    proposals = InvitationProposalSummarySerializer(
        many=True,
        read_only=True,
        help_text="Proposals for which conflicts can be declared",
    )


class InvitationAcceptSerializer(serializers.Serializer):
    """Serializer for accepting a reviewer invitation.

    Optionally includes self-declared conflicts with proposals.
    """

    declared_conflicts = SelfDeclaredConflictSerializer(
        many=True,
        required=False,
        help_text="Optional list of self-declared conflicts with proposals. "
        "Each conflict creates a ConflictOfInterest record with detection_method='self_disclosed'.",
    )


class InvitationDeclineSerializer(serializers.Serializer):
    """Serializer for declining a reviewer invitation."""

    reason = serializers.CharField(
        required=True,
        help_text="Reason for declining the invitation",
    )


# Response serializers for invitation actions
class InvitationAcceptResponseSerializer(serializers.Serializer):
    """Response for successful invitation acceptance."""

    detail = serializers.CharField()
    declared_conflicts = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="UUIDs of created conflict records",
    )


class InvitationAcceptErrorSerializer(serializers.Serializer):
    """Response for invitation acceptance errors (400)."""

    error = serializers.CharField()
    message = serializers.CharField()
    profile_url = serializers.CharField(required=False)


class InvitationAuthErrorSerializer(serializers.Serializer):
    """Response for unauthenticated invitation acceptance (401)."""

    error = serializers.CharField()


class InvitationDeclineResponseSerializer(serializers.Serializer):
    """Response for successful invitation decline."""

    detail = serializers.CharField()


# =============================================================================
# Assignment Batch Serializers (Stage 2 - Proposal Assignment Workflow)
# =============================================================================


class CallAssignmentConfigurationSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for per-call assignment configuration."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")

    class Meta:
        model = models.CallAssignmentConfiguration
        fields = [
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "auto_reassign_on_decline",
            "max_auto_reassign_attempts",
            "assignment_expiration_days",
            "send_reminder_before_expiry_days",
            "created",
            "modified",
        ]
        read_only_fields = ["call"]
        extra_kwargs = {
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
        }


class AssignmentItemSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for individual assignment items within a batch."""

    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    # The deadline is applied here rather than waiting for the expiry task,
    # so an item that can no longer be answered never reads as pending.
    status = serializers.ChoiceField(
        source="effective_status",
        choices=AssignmentItemStatuses.CHOICES,
        read_only=True,
    )
    status_display = serializers.CharField(
        source="get_effective_status_display", read_only=True
    )
    expires_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Deadline for responding to this item (its batch's deadline).",
    )
    is_expired = serializers.BooleanField(
        read_only=True,
        help_text="Whether the item can no longer be answered because its "
        "deadline has passed.",
    )
    review_uuid = serializers.UUIDField(source="review.uuid", read_only=True)
    coi_count = serializers.SerializerMethodField()
    overridden_by_name = serializers.ReadOnlyField(
        source="overridden_by.full_name", default=""
    )

    class Meta:
        model = models.AssignmentItem
        fields = [
            "url",
            "uuid",
            "batch",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "proposal_slug",
            "status",
            "status_display",
            "expires_at",
            "is_expired",
            "affinity_score",
            "has_coi",
            "coi_count",
            "responded_at",
            "decline_reason",
            "review",
            "review_uuid",
            "reassign_count",
            "override_reason",
            "overridden_by_name",
            "overridden_at",
            "created",
        ]
        read_only_fields = [
            "batch",
            "proposal",
            "status",
            "affinity_score",
            "has_coi",
            "responded_at",
            "review",
            "reassign_count",
            "override_reason",
            "overridden_at",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "batch": {
                "lookup_field": "uuid",
                "view_name": "assignment-batch-detail",
            },
            "proposal": {
                "lookup_field": "uuid",
                "view_name": "proposal-proposal-detail",
            },
            "review": {
                "lookup_field": "uuid",
                "view_name": "proposal-review-detail",
            },
        }

    def get_coi_count(self, obj) -> int:
        """Count of COI records blocking this assignment."""
        return obj.coi_records.count()


class ExtendDeadlineRequestSerializer(serializers.Serializer):
    """Request serializer for extending assignment batch deadline."""

    expires_at = serializers.DateTimeField(
        help_text=_("New expiration date and time for the assignment batch.")
    )

    def validate_expires_at(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError(_("New deadline must be in the future."))
        return value


class ExtendDeadlineResponseSerializer(serializers.Serializer):
    """Response serializer confirming the new deadline."""

    expires_at = serializers.DateTimeField(help_text=_("The updated expiration date."))
    status = serializers.CharField(help_text=_("Current status of the batch."))


class AssignmentBatchSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    """Serializer for assignment batches sent to reviewers."""

    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="call.name")
    reviewer_name = serializers.SerializerMethodField()
    reviewer_email = serializers.SerializerMethodField()
    reviewer_uuid = serializers.SerializerMethodField()
    reviewer_pool_entry_uuid = serializers.UUIDField(
        source="reviewer_pool_entry.uuid", read_only=True
    )
    # A sent batch past its deadline reads as expired before the expiry task
    # has stored that.
    status = serializers.ChoiceField(
        source="effective_status",
        choices=AssignmentBatchStatuses.CHOICES,
        read_only=True,
    )
    status_display = serializers.CharField(
        source="get_effective_status_display", read_only=True
    )
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    created_by_name = serializers.ReadOnlyField(source="created_by.full_name")
    items = AssignmentItemSerializer(many=True, read_only=True)
    items_count = serializers.SerializerMethodField()
    items_pending_count = serializers.SerializerMethodField()
    items_accepted_count = serializers.SerializerMethodField()
    items_declined_count = serializers.SerializerMethodField()
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.AssignmentBatch
        fields = [
            "url",
            "uuid",
            "call",
            "call_uuid",
            "call_name",
            "reviewer_pool_entry",
            "reviewer_pool_entry_uuid",
            "reviewer_name",
            "reviewer_email",
            "reviewer_uuid",
            "status",
            "status_display",
            "sent_at",
            "expires_at",
            "responded_at",
            "source",
            "source_display",
            "created_by",
            "created_by_name",
            "manager_notes",
            "items",
            "items_count",
            "items_pending_count",
            "items_accepted_count",
            "items_declined_count",
            "is_expired",
            "created",
        ]
        read_only_fields = [
            "call",
            "reviewer_pool_entry",
            "status",
            "sent_at",
            "expires_at",
            "responded_at",
            "source",
            "created_by",
            "items",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "call": {
                "lookup_field": "uuid",
                "view_name": "proposal-protected-call-detail",
            },
            "reviewer_pool_entry": {
                "lookup_field": "uuid",
                "view_name": "call-reviewer-pool-detail",
            },
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
        }

    def get_reviewer_name(self, obj) -> str | None:
        """Get reviewer name from pool entry."""
        if obj.reviewer_pool_entry.reviewer:
            return obj.reviewer_pool_entry.reviewer.user.full_name
        if obj.reviewer_pool_entry.invited_user:
            return obj.reviewer_pool_entry.invited_user.full_name
        return None

    def get_reviewer_email(self, obj) -> str | None:
        """Get reviewer email from pool entry."""
        if obj.reviewer_pool_entry.reviewer:
            return obj.reviewer_pool_entry.reviewer.user.email
        if obj.reviewer_pool_entry.invited_user:
            return obj.reviewer_pool_entry.invited_user.email
        return obj.reviewer_pool_entry.invited_email

    def get_reviewer_uuid(self, obj) -> str | None:
        """Get reviewer profile UUID if available."""
        if obj.reviewer_pool_entry.reviewer:
            return str(obj.reviewer_pool_entry.reviewer.uuid)
        return None

    def get_items_count(self, obj) -> int:
        """Total count of items in batch."""
        return obj.items.count()

    def get_items_pending_count(self, obj) -> int:
        """Count of pending items."""
        return obj.items_pending_count

    def get_items_accepted_count(self, obj) -> int:
        """Count of accepted items."""
        return obj.items_accepted_count

    def get_items_declined_count(self, obj) -> int:
        """Count of declined items."""
        return obj.items_declined_count


class AssignmentBatchListSerializer(AssignmentBatchSerializer):
    """Lightweight serializer for listing assignment batches (without nested items)."""

    class Meta(AssignmentBatchSerializer.Meta):
        fields = [f for f in AssignmentBatchSerializer.Meta.fields if f != "items"]


# Action serializers for assignment endpoints


class GenerateAssignmentsSerializer(serializers.Serializer):
    """Serializer for generating assignment batches from the algorithm."""

    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Specific proposal UUIDs to generate assignments for. "
        "If empty, generates for all submitted proposals needing reviewers.",
    )
    reviewers_per_proposal = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=20,
        help_text="Number of reviewers to assign per proposal. If not "
        "specified, uses the call's matching configuration "
        "min_reviewers_per_proposal (3 when the call has none). May not exceed "
        "its max_reviewers_per_proposal.",
    )


class SkippedProposalSerializer(serializers.Serializer):
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    reason = serializers.CharField()


class GenerateAssignmentsResponseSerializer(serializers.Serializer):
    """Response for generate_assignments action."""

    batches_created = serializers.IntegerField()
    items_created = serializers.IntegerField()
    proposals_processed = serializers.IntegerField()
    skipped_proposals = SkippedProposalSerializer(
        many=True,
        help_text="Proposals that were skipped with reasons",
    )


class SendAssignmentBatchSerializer(serializers.Serializer):
    """Serializer for sending an assignment batch invitation."""

    manager_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes to include in the invitation email",
    )


class SendAssignmentBatchResponseSerializer(serializers.Serializer):
    """Response for send assignment batch action."""

    detail = serializers.CharField()
    expires_at = serializers.DateTimeField()


class SendAllAssignmentBatchesSerializer(serializers.Serializer):
    """Serializer for sending all draft assignment batches."""

    batch_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="Specific batch UUIDs to send. If empty, sends all draft batches.",
    )


class SendAllAssignmentBatchesResponseSerializer(serializers.Serializer):
    """Response for send_all_batches action."""

    batches_sent = serializers.IntegerField()
    skipped = serializers.IntegerField()


class AssignmentItemAcceptSerializer(serializers.Serializer):
    """Serializer for accepting an assignment item."""

    # No fields needed - just confirms acceptance
    pass


class AssignmentItemDeclineSerializer(serializers.Serializer):
    """Serializer for declining an assignment item."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Reason for declining this assignment",
    )


class AssignmentItemResponseSerializer(serializers.Serializer):
    """Response for assignment item accept/decline actions."""

    detail = serializers.CharField()
    review_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="UUID of created review (only on accept)",
    )


class ReassignItemSerializer(serializers.Serializer):
    """Serializer for reassigning a declined item to another reviewer."""

    reviewer_pool_entry_uuid = serializers.UUIDField(
        help_text="UUID of the pool entry for the new reviewer",
    )
    manager_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Notes to include in the reassignment notification",
    )


class ReassignItemResponseSerializer(serializers.Serializer):
    """Response for reassign action."""

    detail = serializers.CharField()
    new_item_uuid = serializers.UUIDField()
    new_batch_uuid = serializers.UUIDField()


class ReviewerSuggestionItemSerializer(serializers.Serializer):
    pool_entry_uuid = serializers.UUIDField()
    reviewer_name = serializers.CharField()
    affinity_score = serializers.FloatField(allow_null=True)
    current_assignments = serializers.IntegerField()
    max_assignments = serializers.IntegerField()


class SuggestAlternativeReviewersSerializer(serializers.Serializer):
    """Response for suggesting alternative reviewers for a declined item."""

    suggestions = ReviewerSuggestionItemSerializer(
        many=True,
        help_text="List of alternative reviewers with affinity scores",
    )


# Serializer for reviewer's view of their own assignments
class MyAssignmentBatchSerializer(serializers.Serializer):
    """Serializer for reviewer's view of their pending assignment batches."""

    uuid = serializers.UUIDField()
    call_uuid = serializers.UUIDField()
    call_name = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    sent_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(allow_null=True)
    is_expired = serializers.BooleanField()
    items_count = serializers.IntegerField()
    items_pending_count = serializers.IntegerField()
    manager_notes = serializers.CharField(allow_blank=True)


class MyAssignmentItemSerializer(serializers.Serializer):
    """Serializer for individual assignment items in reviewer's view."""

    uuid = serializers.UUIDField()
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    proposal_slug = serializers.CharField()
    proposal_summary = serializers.CharField(allow_blank=True)
    status = serializers.CharField(
        help_text="Effective status: a pending item past its deadline reads as expired."
    )
    status_display = serializers.CharField()
    expires_at = serializers.DateTimeField(
        allow_null=True, help_text="Deadline for responding to this item."
    )
    is_expired = serializers.BooleanField(
        help_text="Whether the item can no longer be answered because its "
        "deadline has passed."
    )
    affinity_score = serializers.FloatField(allow_null=True)
    has_coi = serializers.BooleanField()


class MyAssignmentBatchDetailSerializer(MyAssignmentBatchSerializer):
    """Detailed serializer for reviewer's view of a specific batch."""

    items = MyAssignmentItemSerializer(many=True)


class CreateManualAssignmentSerializer(serializers.Serializer):
    """Serializer for creating a manual assignment batch."""

    reviewer_pool_entry_uuid = serializers.UUIDField(
        help_text="UUID of the reviewer pool entry to assign proposals to",
    )
    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="List of proposal UUIDs to assign to the reviewer",
    )
    manager_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes about this assignment",
    )
    override_workload_limit = serializers.BooleanField(
        default=False,
        help_text="Assign even if this takes the reviewer above their "
        "maximum number of open assignments. The override is logged.",
    )


class CreateManualAssignmentResponseSerializer(serializers.Serializer):
    """Response for create_manual_assignment action."""

    batch_uuid = serializers.UUIDField()
    items_created = serializers.IntegerField()
    skipped_proposals = SkippedProposalSerializer(
        many=True,
        help_text="Proposals that were skipped with reasons",
    )


class NotifyProposalsSerializer(serializers.Serializer):
    """A call manager's message to the applicants of selected proposals."""

    RECIPIENT_CHOICES = (
        ("applicant", "The applicant, with the roles the call copies"),
        ("team", "Everyone on the proposal team"),
    )

    proposal_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="Proposals of this call whose applicants receive the message",
    )
    recipients = serializers.ChoiceField(
        choices=RECIPIENT_CHOICES,
        default="applicant",
        help_text="Who receives the message on each proposal",
    )
    subject = serializers.CharField(max_length=255)
    body = serializers.CharField()

    def validate_subject(self, value):
        # A line break is not allowed in a mail header: the send would fail.
        if "\n" in value or "\r" in value:
            raise serializers.ValidationError(_("Subject must be a single line."))
        return value


class NotifyProposalsResponseSerializer(serializers.Serializer):
    """Response for notify_proposals action."""

    proposals_notified = serializers.IntegerField()
    skipped_proposals = SkippedProposalSerializer(
        many=True,
        help_text="Requested proposals that are not part of this call",
    )


# =============================================================================
# Workflow Step Serializers
# =============================================================================


class CallProposalRoleSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """An evaluator role of a call, such as a lead or second rapporteur."""

    uuid = serializers.UUIDField(read_only=True, format="hex")
    call_uuid = serializers.UUIDField(source="call.uuid", read_only=True, format="hex")
    key = serializers.SlugField(
        max_length=64,
        required=False,
        help_text=(
            "Identifier of the role within its call. Derived from the name "
            "when left out. Cannot change once the role exists."
        ),
    )

    eligible_count = serializers.SerializerMethodField(
        help_text=(
            "How many people may hold the role on the call now; null when any user may."
        )
    )
    holders_warning = serializers.SerializerMethodField(
        help_text=(
            "Why the call cannot staff the role on a proposal: fewer people "
            "may hold it than its minimum of holders. Null when it can."
        )
    )

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_eligible_count(self, role):
        return proposal_roles.eligible_count(role)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_holders_warning(self, role):
        return proposal_roles.holders_shortfall(role)

    class Meta:
        model = models.CallProposalRole
        fields = [
            "uuid",
            "created",
            "modified",
            "call_uuid",
            "key",
            "name",
            "description",
            "eligible_from",
            "min_holders",
            "max_holders",
            "allows_lead",
            "eligible_count",
            "holders_warning",
        ]
        read_only_fields = ("uuid", "created", "modified")
        protected_fields = ("call", "key")
        # (call, key) is checked in validate(): the call is set by the view.
        validators = []
        extra_kwargs = {
            "min_holders": {"max_value": models.MAX_STEP_PEOPLE},
            "max_holders": {"max_value": models.MAX_STEP_PEOPLE},
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        call = attrs.get("call") or getattr(self.instance, "call", None)
        key = attrs.get("key")
        if (
            self.instance is None
            and key
            and call is not None
            and models.CallProposalRole.objects.filter(call=call, key=key).exists()
        ):
            raise serializers.ValidationError(
                {"key": _("The call already has a role with this key.")}
            )
        if (
            self.instance is not None
            and self.instance.allows_lead
            and attrs.get("allows_lead") is False
            and self.instance.lead_only_steps()
        ):
            raise serializers.ValidationError(
                {
                    "allows_lead": _(
                        "Only the lead of this role contributes to some of the "
                        "call's steps; the role keeps its lead while they do."
                    )
                }
            )
        eligible_from = attrs.get(
            "eligible_from", getattr(self.instance, "eligible_from", None)
        )
        if (
            eligible_from == enums.ProposalRoleEligibility.PANEL_CHAIR
            and attrs.get("min_holders", getattr(self.instance, "min_holders", 1)) > 1
        ):
            raise serializers.ValidationError(
                {
                    "min_holders": _(
                        "A panel has one chair: a role for the chair of the "
                        "proposal's panel needs at most one holder."
                    )
                }
            )
        candidate = models.CallProposalRole(
            min_holders=attrs.get(
                "min_holders", getattr(self.instance, "min_holders", 1)
            ),
            max_holders=attrs.get(
                "max_holders", getattr(self.instance, "max_holders", None)
            ),
        )
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        if self.instance is not None and "max_holders" in attrs:
            short = self.instance.steps_beyond_holders(attrs["max_holders"])
            if short:
                raise serializers.ValidationError(
                    {
                        "max_holders": _(
                            "Step '%(step)s' needs %(count)s contributions "
                            "from this role's holders; lower its minimum "
                            "first, or keep enough holders."
                        )
                        % {
                            "step": short[0].name,
                            "count": short[0].min_contributions,
                        }
                    }
                )
        return attrs


class ProposalRoleAssignmentSerializer(serializers.ModelSerializer):
    """Who holds an evaluator role on a proposal, and how they came to."""

    uuid = serializers.UUIDField(read_only=True, format="hex")
    proposal_uuid = serializers.UUIDField(
        source="proposal.uuid", read_only=True, format="hex"
    )
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    proposal_state = serializers.ReadOnlyField(source="proposal.state")
    call_uuid = serializers.UUIDField(
        source="proposal.round.call.uuid", read_only=True, format="hex"
    )
    call_name = serializers.ReadOnlyField(source="proposal.round.call.name")
    role_uuid = serializers.UUIDField(source="role.uuid", read_only=True, format="hex")
    role_key = serializers.ReadOnlyField(source="role.key")
    role_name = serializers.ReadOnlyField(source="role.name")
    user_uuid = serializers.UUIDField(source="user.uuid", read_only=True, format="hex")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_email = serializers.ReadOnlyField(
        source="user.email",
        allow_null=True,
        help_text="Shown to staff, those overseeing the call and the holder.",
    )
    assigned_by_full_name = serializers.SerializerMethodField()
    assigned_in_step_name = serializers.SerializerMethodField()
    revoked_by_full_name = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(read_only=True)
    coi_confirmation_required = serializers.BooleanField(
        read_only=True, allow_null=True
    )
    coi_declaration_only = serializers.BooleanField(read_only=True, allow_null=True)
    coi_declaration_text = serializers.SerializerMethodField(
        help_text=(
            "The conflict-of-interest declaration the holder confirms: as "
            "confirmed once they did, else as worded now. Empty for the "
            "platform's standard declaration, or when none is asked for."
        )
    )
    awaits_coi_confirmation = serializers.BooleanField(read_only=True, allow_null=True)
    has_standing_conflict = serializers.SerializerMethodField(
        help_text=(
            "A conflict of interest is recorded between the holder and the "
            "proposal and not dismissed or waived: the role gives no access "
            "meanwhile."
        )
    )

    # Another holder's email and conflict-of-interest state are for staff,
    # those overseeing the call and the holder alone; so is why their role
    # was revoked (``revoked_reason``, blanked for anyone else).
    PRIVATE_FIELDS = (
        "user_email",
        "coi_confirmation_required",
        "coi_confirmed_at",
        "coi_declaration_only",
        "coi_declaration_text",
        "awaits_coi_confirmation",
        "has_standing_conflict",
    )

    class Meta:
        model = models.ProposalRoleAssignment
        fields = [
            "uuid",
            "created",
            "proposal_uuid",
            "proposal_name",
            "proposal_slug",
            "proposal_state",
            "call_uuid",
            "call_name",
            "role_uuid",
            "role_key",
            "role_name",
            "user_uuid",
            "user_full_name",
            "user_email",
            "is_lead",
            "assigned_by_full_name",
            "assigned_in_step_name",
            "revoked_at",
            "revoked_by_full_name",
            "revoked_reason",
            "coi_confirmation_required",
            "coi_confirmed_at",
            "coi_declaration_only",
            "coi_declaration_text",
            "is_active",
            "awaits_coi_confirmation",
            "has_standing_conflict",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.context.get("full_detail"):
            return data
        request = self.context.get("request")
        if request is not None and instance.user_id == request.user.id:
            return data
        for field in self.PRIVATE_FIELDS:
            data[field] = None
        # Why a role was taken away -- a recusal for a conflict of interest,
        # say -- is for the same few.
        data["revoked_reason"] = ""
        return data

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_has_standing_conflict(self, obj):
        if hasattr(obj, "has_standing_conflict"):
            return obj.has_standing_conflict
        return proposal_roles.recorded_conflicts(obj.user, obj.proposal).exists()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_coi_declaration_text(self, obj):
        if obj.coi_confirmed_at is not None:
            return obj.coi_declaration_text
        if not obj.coi_confirmation_required:
            return ""
        return coi_declarations.for_assignment(obj)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_assigned_by_full_name(self, obj):
        return obj.assigned_by.full_name if obj.assigned_by else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_assigned_in_step_name(self, obj):
        return obj.assigned_in.call_step.name if obj.assigned_in else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_revoked_by_full_name(self, obj):
        return obj.revoked_by.full_name if obj.revoked_by else None


class ProposalRoleAssignSerializer(serializers.Serializer):
    role_uuid = serializers.UUIDField(help_text="The call's evaluator role.")
    user_uuid = serializers.UUIDField(help_text="The user to hold it.")
    is_lead = serializers.BooleanField(
        default=False,
        help_text="Mark the holder as the role's lead, in place of any other.",
    )


class ProposalRoleAssignmentActionSerializer(serializers.Serializer):
    assignment_uuid = serializers.UUIDField()


class ProposalRoleCandidateSerializer(serializers.Serializer):
    """A user who may be assigned an evaluator role on a proposal."""

    uuid = serializers.UUIDField(format="hex")
    full_name = serializers.CharField()
    email = serializers.CharField(
        allow_null=True,
        help_text="Shown to staff and those overseeing the call only.",
    )
    username = serializers.CharField()
    holds_role = serializers.BooleanField(
        help_text="Whether the user already holds the role on the proposal."
    )
    holds_other_role = serializers.BooleanField(
        help_text=(
            "The user holds another evaluator role on the proposal; one "
            "person holds one, so they cannot be assigned."
        )
    )
    held_role_name = serializers.CharField(
        allow_null=True,
        help_text="The evaluator role the user holds on the proposal, if any.",
    )
    has_recorded_conflict = serializers.BooleanField(
        allow_null=True,
        help_text=(
            "A conflict of interest is recorded for the user and the "
            "proposal; they cannot be assigned. Shown to staff and those "
            "overseeing the call only, null for anyone else."
        ),
    )
    has_reviewer_profile = serializers.BooleanField(
        help_text=(
            "Without a reviewer profile no conflict can be on record: the "
            "user is checked by their own declaration only."
        )
    )


class WorkflowCriterionSerializer(serializers.ModelSerializer):
    uuid = serializers.UUIDField(read_only=True)

    class Meta:
        model = models.WorkflowCriterion
        fields = ["uuid", "name", "order", "weight"]


class CallWorkflowStepNotificationRuleSerializer(
    serializers.HyperlinkedModelSerializer
):
    workflow_step = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallWorkflowStep.objects.all(),
    )
    workflow_step_uuid = serializers.UUIDField(
        source="workflow_step.uuid", read_only=True, format="hex"
    )
    step = serializers.ReadOnlyField(source="workflow_step.key")
    call_uuid = serializers.UUIDField(
        source="workflow_step.call.uuid", read_only=True, format="hex"
    )
    notified_proposal_roles = serializers.SlugRelatedField(
        slug_field="name",
        many=True,
        required=False,
        queryset=Role.objects.filter(
            is_active=True,
            content_type__app_label="proposal",
            content_type__model="proposal",
        ),
        help_text=(
            "Only for an applicant audience: names of the proposal roles whose "
            "holders are notified. Empty notifies the proposal creator and "
            "every member of the proposal team."
        ),
    )
    proposal_role = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallProposalRole.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "Only for the proposal_role audience: the call's evaluator role "
            "whose holders are notified."
        ),
    )
    proposal_role_name = serializers.ReadOnlyField(
        source="proposal_role.name", allow_null=True
    )

    class Meta:
        model = models.CallWorkflowStepNotificationRule
        fields = [
            "url",
            "uuid",
            "created",
            "modified",
            "workflow_step",
            "workflow_step_uuid",
            "step",
            "call_uuid",
            "trigger",
            "recipient",
            "days_before",
            "is_enabled",
            "notified_proposal_roles",
            "proposal_role",
            "proposal_role_name",
        ]
        read_only_fields = ("uuid", "created", "modified")
        # Uniqueness of the audience is checked in validate(): an evaluator
        # role is part of it, and may be empty.
        validators = []
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "call-workflow-step-notification-rule-detail",
            },
        }

    def validate_workflow_step(self, workflow_step):
        if self.instance and self.instance.workflow_step_id != workflow_step.id:
            raise serializers.ValidationError(
                _("A rule cannot be moved to another workflow step.")
            )
        if not permissions_utils.has_permission_on_any_source(
            self.context["request"],
            permissions_enums.PermissionEnum.UPDATE_CALL,
            workflow_step.call,
            proposal_permissions.CALL_PERMISSION_SOURCES,
        ):
            raise PermissionDenied()
        if workflow_step.call.state == CallStates.ARCHIVED:
            raise serializers.ValidationError(_("Cannot modify an archived call."))
        return workflow_step

    def validate(self, attrs):
        attrs = super().validate(attrs)
        get = lambda name: attrs.get(  # noqa: E731
            name, getattr(self.instance, name, None) if self.instance else None
        )
        trigger = get("trigger")
        days_before = get("days_before")
        workflow_step = get("workflow_step")
        recipient = get("recipient")

        if trigger == NotificationRuleTriggers.DEADLINE_APPROACHING:
            if days_before is None:
                raise serializers.ValidationError(
                    {"days_before": _("Required for the deadline_approaching trigger.")}
                )
        elif days_before is not None:
            raise serializers.ValidationError(
                {"days_before": _("Only applies to the deadline_approaching trigger.")}
            )

        # Applicants are never told about internal evaluation steps.
        if (
            workflow_step is not None
            and recipient == NotificationRuleRecipients.APPLICANT
            and notification_rules.is_internal_step(workflow_step)
        ):
            raise serializers.ValidationError(
                {
                    "recipient": _(
                        "The applicant cannot be notified about internal "
                        "evaluation steps."
                    )
                }
            )

        if "notified_proposal_roles" in attrs:
            roles = attrs["notified_proposal_roles"]
        elif self.instance:
            roles = list(self.instance.notified_proposal_roles.all())
        else:
            roles = []
        if (
            roles
            and workflow_step is not None
            and not notification_rules.is_applicant_recipient(recipient, workflow_step)
        ):
            raise serializers.ValidationError(
                {
                    "notified_proposal_roles": _(
                        "Proposal roles can only be chosen for a rule that "
                        "notifies the applicant."
                    )
                }
            )

        proposal_role = get("proposal_role")
        if recipient in NotificationRuleRecipients.NAMING_PROPOSAL_ROLE:
            if proposal_role is None:
                raise serializers.ValidationError(
                    {"proposal_role": _("Choose the evaluator role to notify.")}
                )
            if (
                workflow_step is not None
                and proposal_role.call_id != workflow_step.call_id
            ):
                raise serializers.ValidationError(
                    {"proposal_role": _("The evaluator role belongs to another call.")}
                )
            if (
                recipient == NotificationRuleRecipients.PROPOSAL_ROLE_LEAD
                and not proposal_role.allows_lead
            ):
                raise serializers.ValidationError(
                    {"proposal_role": _("The evaluator role has no lead to notify.")}
                )
        elif "proposal_role" in attrs and attrs["proposal_role"] is not None:
            raise serializers.ValidationError(
                {
                    "proposal_role": _(
                        "An evaluator role can only be chosen for a rule that "
                        "notifies the holders of one."
                    )
                }
            )
        else:
            attrs["proposal_role"] = None
            proposal_role = None

        duplicates = models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step=workflow_step,
            trigger=trigger,
            recipient=recipient,
            proposal_role=proposal_role,
        )
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError(
                _("The step already has a rule for this event and audience.")
            )
        return attrs


class CallWorkflowStepNotificationRuleNestedSerializer(serializers.ModelSerializer):
    notified_proposal_roles = serializers.SlugRelatedField(
        slug_field="name", many=True, read_only=True
    )
    proposal_role = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    proposal_role_name = serializers.ReadOnlyField(
        source="proposal_role.name", allow_null=True
    )

    class Meta:
        model = models.CallWorkflowStepNotificationRule
        fields = [
            "uuid",
            "trigger",
            "recipient",
            "days_before",
            "is_enabled",
            "notified_proposal_roles",
            "proposal_role",
            "proposal_role_name",
        ]
        read_only_fields = fields


class WorkflowStepUserOfferingSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    name = serializers.CharField()
    customer_uuid = serializers.UUIDField(format="hex")


class WorkflowStepResponsibleUserSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    username = serializers.CharField()
    full_name = serializers.CharField()
    email = serializers.EmailField()
    is_panel_chair = serializers.BooleanField()
    is_panel_vice_chair = serializers.BooleanField()
    offerings = WorkflowStepUserOfferingSerializer(
        many=True,
        help_text=(
            "The offerings accepted into the call that the user manages; "
            "empty unless the user takes part as an offering manager."
        ),
    )


# The applicant answers an award once it is decided.
AWARD_BEFORE_DECISION = _(
    "The award response must come after the allocation decision: the "
    "applicant answers an award once it has been decided."
)

UNGUARDED_AUTOMATIC_DECISION = _(
    "An automatic allocation decision needs an enabled step before it that "
    "can reject a proposal, such as a technical assessment; otherwise every "
    "submission would be awarded."
)


class StepRunConditionSerializer(serializers.Serializer):
    """The shape of a step's run condition: the step and either its
    outcomes or a score spread, or nothing to run on every proposal."""

    step = serializers.CharField(
        required=False,
        help_text="Key of the earlier step whose outcome or scores decide.",
    )
    outcomes = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=(
            "Outcomes of that step, or of its contributions, on which this "
            "step runs; 'expired' when it lapsed or was skipped."
        ),
    )
    score_spread_above = serializers.FloatField(
        required=False,
        min_value=0,
        help_text=(
            "In place of outcomes: the step runs only when the highest and the "
            "lowest score that step's contributions gave the proposal differ "
            "by more than this many points."
        ),
    )


@extend_schema_field(StepRunConditionSerializer)
class RunConditionField(serializers.JSONField):
    """A step's run condition; empty (or null) to run on every proposal."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        return {} if value is None else value

    def to_representation(self, value):
        return super().to_representation(value or {})


class StepAudienceStatusLabelsSerializer(serializers.Serializer):
    """One audience's labels of a call step; each left out keeps the
    computed label. Other than the applicant's, a label may show counts of
    the step's work as placeholders: {invited}, {accepted}, {declined},
    {submitted}, {expected}, {reports_submitted}, {reports_expected}."""

    active = serializers.CharField(
        required=False, help_text="While the step runs on the proposal."
    )
    completed = serializers.CharField(
        required=False, help_text="Once the step has ended on the proposal."
    )
    changes_requested = serializers.CharField(
        required=False,
        help_text="While the step has sent the proposal back for changes.",
    )
    outcomes = serializers.DictField(
        child=serializers.CharField(),
        required=False,
        help_text=(
            "Once the step has ended with an outcome the reader is shown, by "
            "outcome; in place of 'completed'."
        ),
    )


class StepStatusLabelsSerializer(serializers.Serializer):
    applicant = StepAudienceStatusLabelsSerializer(
        required=False,
        help_text=(
            "What the applicant team's dashboard and tracker call the step; "
            "only while the step is visible to the applicant."
        ),
    )
    evaluators = StepAudienceStatusLabelsSerializer(
        required=False,
        help_text=(
            "What the evaluators' dashboards call the step; also the call "
            "team's and the offering teams', state by state, where theirs "
            "are not set."
        ),
    )
    call_team = StepAudienceStatusLabelsSerializer(
        required=False,
        help_text=(
            "What the call team's dashboard (call managers, staff, support) "
            "calls the step."
        ),
    )
    offering_team = StepAudienceStatusLabelsSerializer(
        required=False,
        help_text=(
            "What an offering team's dashboard calls the step, on the steps it "
            "works on."
        ),
    )


@extend_schema_field(StepStatusLabelsSerializer)
class StatusLabelsField(serializers.JSONField):
    """A step's status labels; empty (or null) to keep the computed labels."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        return {} if value is None else value

    def to_representation(self, value):
        return super().to_representation(value or {})


class CallWorkflowStepSerializer(
    CallNotArchivedCreateMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    call_uuid = serializers.ReadOnlyField(source="call.uuid")
    call_name = serializers.ReadOnlyField(source="call.name")

    key = serializers.SlugField(
        max_length=64,
        required=False,
        help_text=(
            "Identifier of the step within its call. Derived from the name "
            "when left out. Cannot change once the step exists."
        ),
    )
    # Deprecated: the step was named by its catalogue key before steps had
    # a kind. Kept for one release, read and write.
    step = serializers.SlugField(
        source="key",
        max_length=64,
        required=False,
        help_text="Deprecated alias of key.",
    )
    kind = serializers.ChoiceField(
        choices=StepKinds.CHOICES,
        required=False,
        help_text=(
            "Behavior of the step; see the workflow_step_kinds endpoint. "
            "Required unless the key is one of the default steps. Cannot "
            "change once the step exists."
        ),
    )
    name = serializers.CharField(max_length=255, required=False)
    order = serializers.IntegerField(
        min_value=0,
        max_value=models.CallWorkflowStep.MAX_ORDER,
        required=False,
        help_text=(
            "Position in the call's workflow. A step already there, and every "
            "step after it, moves one down. Appended when left out."
        ),
    )
    # Deprecated alias of completer, kept for one release.
    responsible_role = serializers.ChoiceField(
        source="completer",
        choices=ResponsibleRoles.CHOICES,
        required=False,
        allow_null=True,
        help_text="Deprecated alias of completer.",
    )
    status_labels = StatusLabelsField(
        required=False,
        allow_null=True,
        help_text=(
            "What each audience's dashboard calls the step, as entered: "
            "per audience, while active, once completed, per outcome and while "
            "changes are requested. Left out, the computed label stays."
        ),
    )
    allowed_outcomes = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Outcomes the step can be completed with.",
    )
    checklist = CallFormField(
        slug_field="uuid",
        queryset=checklist_models.Checklist.objects.all(),
        required=False,
        allow_null=True,
    )
    checklist_name = serializers.SerializerMethodField()
    completer_proposal_role = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallProposalRole.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "The call's evaluator role whose holders complete the step; "
            "required when the completer is proposal_role."
        ),
    )
    completer_proposal_role_name = serializers.ReadOnlyField(
        source="completer_proposal_role.name", allow_null=True
    )
    assigns_roles = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallProposalRole.objects.all(),
        many=True,
        required=False,
        help_text=(
            "Assignment step: the call's evaluator roles whose holders the "
            "completer picks, such as a lead and a second rapporteur."
        ),
    )
    assigns_roles_names = serializers.SerializerMethodField()
    contributor_proposal_role_uuid = serializers.SerializerMethodField(
        help_text=(
            "The evaluator role the contributor source names, when it names one."
        )
    )
    delegable_proposal_role_uuid = serializers.SerializerMethodField(
        help_text=(
            "The evaluator role delegable_to names, when it names one: its "
            "holders on the proposal can be handed a slot."
        )
    )
    input_steps = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.CallWorkflowStep.objects.all(),
        many=True,
        required=False,
        help_text=(
            "Earlier steps of the call whose outputs this step's contributors "
            "and completer read; on an applicant step, whose public parts the "
            "applicant reads."
        ),
    )
    input_steps_names = serializers.SerializerMethodField()
    is_mandatory = serializers.SerializerMethodField()
    contributor_source = serializers.CharField(
        max_length=64,
        required=False,
        help_text=(
            "Who contributes to the step, each with a contribution of their "
            "own: none, requested_offering_team, assigned_reviewers, "
            "proposal_role:<id> or call_role:<call_manager|reviewer|panel_member>."
        ),
    )
    # Deprecated alias of min_contributions, kept for one release.
    min_reviewers = serializers.IntegerField(
        source="min_contributions",
        min_value=0,
        max_value=models.MAX_STEP_PEOPLE,
        required=False,
        allow_null=True,
        help_text="Deprecated alias of min_contributions.",
    )
    criteria = WorkflowCriterionSerializer(many=True, required=False)
    criteria_locked = serializers.SerializerMethodField(
        help_text=(
            "Whether reviewers have scored the step's criteria or submitted a "
            "review in it. Its criteria can then be reordered, not changed."
        )
    )
    notification_rules = CallWorkflowStepNotificationRuleNestedSerializer(
        many=True, read_only=True
    )
    responsible_users = serializers.SerializerMethodField()
    contributing_users = serializers.SerializerMethodField(
        help_text=(
            "Who the contributor source names on the call: the holders of the "
            "call role it names, the reviewers who accepted to join the "
            "reviewer pool, or the managers of the offerings accepted into the "
            "call. Empty for an evaluator role, which is held per proposal."
        )
    )
    run_condition_step_name = serializers.SerializerMethodField(
        help_text=(
            "Name of the step the run condition names, for display; null "
            "without a condition."
        )
    )
    run_condition = RunConditionField(
        required=False,
        allow_null=True,
        help_text=(
            "When the step runs: empty to run on every proposal; the key of "
            "an earlier step and the outcomes of it, or of its contributions, "
            'on which it runs; or the key of an earlier step and "score_spread_'
            'above": the points by which the highest and lowest scores its '
            "contributions gave must differ for the step to run. A proposal on "
            "which it does not hold skips the step. Fixed for a proposal when "
            "it is submitted."
        ),
    )

    contributors_warning = serializers.SerializerMethodField(
        help_text=(
            "Why the call cannot give the step the contributions it needs: "
            "the evaluator role, call role or reviewer pool it takes them "
            "from has fewer people than its minimum. Null when it can."
        )
    )

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_contributors_warning(self, obj):
        return proposal_roles.contributors_shortfall(obj)

    class Meta:
        model = models.CallWorkflowStep
        fields = [
            "uuid",
            "created",
            "modified",
            "key",
            "step",
            "kind",
            "name",
            "description",
            "order",
            "outcome_set",
            "allowed_outcomes",
            "on_negative",
            "call_uuid",
            "call_name",
            "is_enabled",
            "is_mandatory",
            "is_allocation_decision",
            "duration_in_days",
            "checklist",
            "checklist_name",
            "checklist_required",
            "blind_review",
            "requires_coi_confirmation",
            "coi_declaration_text",
            "contributor_source",
            "contributor_proposal_role_uuid",
            "delegable_proposal_role_uuid",
            "per_requested_offering",
            "contributors_lead_only",
            "delegable_to",
            "min_contributions",
            "max_contributions",
            "min_reviewers",
            "min_score_threshold",
            "applicant_visible",
            "completer",
            "responsible_role",
            "completer_proposal_role",
            "completer_proposal_role_name",
            "assigns_roles",
            "assigns_roles_names",
            "input_steps",
            "input_steps_names",
            "contributors_warning",
            "release_to_applicant",
            "collects_reviewer_suggestions",
            "allows_change_requests",
            "allows_conditional_acceptance",
            "transition_mode",
            "run_condition",
            "run_condition_step_name",
            "parallel_with_previous",
            "round_scoped",
            "phase",
            "project_end_offset_days",
            "status_labels",
            "include_award_response",
            "decides_automatically",
            "automatic_decision_guard",
            "allocation_time",
            "display_order",
            "criteria",
            "criteria_locked",
            "notification_rules",
            "responsible_users",
            "contributing_users",
        ]
        read_only_fields = ("uuid", "created", "modified")
        protected_fields = ("call", "key", "step", "kind")
        # key and its alias step share a model field, so DRF cannot derive
        # the unique-together validators: validate() checks the key, and the
        # order is made room for rather than refused.
        validators = []
        extra_kwargs = {
            "outcome_set": {
                "required": False,
                "help_text": (
                    "Outcomes the step can be completed with; one of its kind's "
                    "outcome sets. The kind's default when left out."
                ),
            },
            "display_order": {"help_text": "Deprecated: steps are ordered by order."},
            "min_contributions": {"max_value": models.MAX_STEP_PEOPLE},
            "max_contributions": {"max_value": models.MAX_STEP_PEOPLE},
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_checklist_name(self, obj):
        return obj.checklist.name if obj.checklist else None

    def _step_users(self, obj, users, offering_managers):
        """``users`` as the step's people: their panel seat and, when they
        take part as offering managers, the offerings they manage."""
        chair_id = obj.call.panel_chair_id
        vice_chair_id = obj.call.panel_vice_chair_id
        offerings = (
            notification_rules.call_offerings_by_manager(obj.call)
            if offering_managers
            else {}
        )
        return [
            {
                "uuid": user.uuid.hex,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "is_panel_chair": user.id == chair_id,
                "is_panel_vice_chair": user.id == vice_chair_id,
                "offerings": [
                    {
                        "uuid": offering.uuid.hex,
                        "name": offering.name,
                        "customer_uuid": offering.customer.uuid.hex,
                    }
                    for offering in offerings.get(user.id, [])
                ],
            }
            for user in users
        ]

    @extend_schema_field(WorkflowStepResponsibleUserSerializer(many=True))
    def get_responsible_users(self, obj):
        """Everyone currently holding the step's completer role — the call
        manager's answer to "who evaluates this step". Provider-side offering
        managers appear once their offering is accepted into the call."""
        return self._step_users(
            obj,
            notification_rules.responsible_users_for_step(obj),
            obj.completer == ResponsibleRoles.OFFERING_MANAGER,
        )

    @extend_schema_field(WorkflowStepResponsibleUserSerializer(many=True))
    def get_contributing_users(self, obj):
        return self._step_users(
            obj,
            notification_rules.contributing_users_for_step(obj),
            obj.contributor_source_kind == ContributorSources.REQUESTED_OFFERING_TEAM,
        )

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_assigns_roles_names(self, obj):
        return [role.name for role in obj.assigns_roles.all()]

    @extend_schema_field(serializers.UUIDField(format="hex", allow_null=True))
    def get_contributor_proposal_role_uuid(self, obj):
        if obj.contributor_source_kind != ContributorSources.PROPOSAL_ROLE:
            return None
        role = proposal_roles.resolve_role(obj.call, obj.contributor_source)
        return role.uuid.hex if role is not None else None

    @extend_schema_field(serializers.UUIDField(format="hex", allow_null=True))
    def get_delegable_proposal_role_uuid(self, obj):
        target, _ = contributions.delegation_target(obj)
        if target != DelegationTargets.PROPOSAL_ROLE:
            return None
        role = proposal_roles.resolve_role(obj.call, obj.delegable_to)
        return role.uuid.hex if role is not None else None

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_input_steps_names(self, obj):
        return [step.name for step in obj.input_steps.all()]

    @extend_schema_field(serializers.BooleanField())
    def get_is_mandatory(self, obj):
        # The allocation decision can't be disabled and is required for call
        # activation; the UI mirrors this to gate the Activate button.
        return obj.is_allocation_decision

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_run_condition_step_name(self, obj):
        key = (obj.run_condition or {}).get("step")
        if not key:
            return None
        source = (
            models.CallWorkflowStep.objects.filter(call_id=obj.call_id, key=key)
            .values_list("name", flat=True)
            .first()
        )
        return source or key

    def validate_run_condition(self, value):
        # Null and empty both mean the step runs on every proposal.
        return value or {}

    def _current(self, attrs, name, default=None):
        if name in attrs:
            return attrs[name]
        if self.instance is not None:
            return getattr(self.instance, name)
        return default

    def _apply_default_step(self, attrs):
        """Fill a new step named by a default key from that default.

        A client written for the fixed catalogue creates a step by its key
        alone; the kind and the rest come from the step it meant.
        """
        default = DEFAULT_CALL_STEPS_MAP.get(attrs.get("key"))
        if default is None or attrs.get("kind", default.kind) != default.kind:
            return
        attrs.setdefault("kind", default.kind)
        attrs.setdefault("name", default.name)
        attrs.setdefault("description", default.description)
        attrs.setdefault("outcome_set", default.outcome_set)
        attrs.setdefault("on_negative", default.on_negative)
        attrs.setdefault("is_allocation_decision", default.is_allocation_decision)
        attrs.setdefault("contributor_source", default.contributor_source)
        if not attrs.get("completer"):
            attrs["completer"] = default.completer

    def validate(self, attrs):
        attrs = super().validate(attrs)
        self._refuse_conflicting_minimums()
        call = self._current(attrs, "call")

        if self.instance is None:
            key = attrs.get("key")
            self._apply_default_step(attrs)
            if not attrs.get("kind"):
                raise serializers.ValidationError(
                    {"kind": _("This field is required.")}
                )
            if not key and not attrs.get("name"):
                raise serializers.ValidationError(
                    {"name": _("Either a name or a key is required.")}
                )
            if (
                key
                and call is not None
                and models.CallWorkflowStep.objects.filter(call=call, key=key).exists()
            ):
                raise serializers.ValidationError(
                    {"key": _("The call already has a step with this key.")}
                )

        kind = enums.STEP_KINDS_MAP[self._current(attrs, "kind")]
        is_allocation_decision = self._current(attrs, "is_allocation_decision", False)
        if self.instance is not None:
            self._validate_allocation_decision_change(attrs, call)

        if attrs.get("include_award_response") and not is_allocation_decision:
            raise serializers.ValidationError(
                {
                    "include_award_response": (
                        "include_award_response can only be set on the "
                        "allocation_decision step."
                    )
                }
            )

        if (
            attrs.get("allocation_time") == AllocationTimes.FIXED_DATE
            and not is_allocation_decision
        ):
            raise serializers.ValidationError(
                {
                    "allocation_time": (
                        "allocation_time can only be configured on the "
                        "allocation_decision step."
                    )
                }
            )
        if (
            is_allocation_decision
            and self._current(attrs, "is_enabled", True)
            and self._current(attrs, "allocation_time") == AllocationTimes.FIXED_DATE
            and isinstance(call, models.Call)
            and call.has_open_ended_round()
        ):
            raise serializers.ValidationError(
                {"allocation_time": models.OPEN_ENDED_ROUND_ALLOCATION_MESSAGE}
            )

        # Leaving the evaluator-role completer behind drops its role.
        if (
            "completer" in attrs
            and attrs["completer"] != ResponsibleRoles.PROPOSAL_ROLE
            and "completer_proposal_role" not in attrs
        ):
            attrs["completer_proposal_role"] = None

        criteria = attrs.get("criteria")
        if criteria and kind.id != StepKinds.ASSESSMENT:
            raise serializers.ValidationError(
                {"criteria": ("Criteria can only be configured on an assessment step.")}
            )

        # A step split per requested offering is its teams' work.
        if (
            attrs.get("per_requested_offering")
            and self._current(attrs, "contributor_source", ContributorSources.NONE)
            == ContributorSources.NONE
        ):
            attrs["contributor_source"] = ContributorSources.REQUESTED_OFFERING_TEAM
        if "contributor_source" in attrs:
            attrs["contributor_source"] = (
                attrs["contributor_source"] or ContributorSources.NONE
            )
        if (
            self.instance is not None
            and any(field in attrs for field in self.LOCKED_ONCE_WALKED)
            and self._has_proposals(call)
        ):
            self._refuse_contributor_change(attrs)
        if self.instance is not None:
            self._refuse_change_under_proposals(attrs)
            self._refuse_form_change_once_reached(attrs)
            self._refuse_criteria_change_once_scored(attrs)
        if attrs.get("checklist") is not None:
            refuse_form_of_another_organisation(
                attrs["checklist"], call.manager if call else None, "checklist"
            )

        # A report written by one lead -- a consolidated report, final
        # feedback -- is meant for the applicant: released unless told not to.
        if (
            self.instance is None
            and "release_to_applicant" not in attrs
            and kind.id == StepKinds.ASSESSMENT
            and str(attrs.get("contributor_source") or "").startswith(
                f"{ContributorSources.PROPOSAL_ROLE}:"
            )
            and attrs.get("max_contributions") == 1
        ):
            attrs["release_to_applicant"] = True

        if (
            attrs.get("decides_automatically") is False
            and "automatic_decision_guard" not in attrs
        ):
            # Turning the automatic decision off drops what guarded it.
            attrs["automatic_decision_guard"] = (
                enums.AutomaticDecisionGuards.EARLIER_STEP
            )
        candidate = models.CallWorkflowStep(
            pk=getattr(self.instance, "pk", None),
            call=call,
            kind=kind.id,
            name=self._current(attrs, "name", "") or kind.name,
            outcome_set=self._current(attrs, "outcome_set", ""),
            on_negative=self._current(attrs, "on_negative", OnNegativeOutcomes.REJECT),
            is_enabled=self._current(attrs, "is_enabled", True),
            is_allocation_decision=is_allocation_decision,
            completer=self._current(attrs, "completer") or kind.default_completer,
            completer_proposal_role=self._current(attrs, "completer_proposal_role"),
            contributor_source=self._current(
                attrs, "contributor_source", ContributorSources.NONE
            ),
            per_requested_offering=self._current(
                attrs, "per_requested_offering", False
            ),
            contributors_lead_only=self._current(
                attrs, "contributors_lead_only", False
            ),
            delegable_to=self._current(attrs, "delegable_to", "") or "",
            min_contributions=self._current(attrs, "min_contributions"),
            max_contributions=self._current(attrs, "max_contributions"),
            key=self._current(attrs, "key", ""),
            run_condition=self._current(attrs, "run_condition", {}),
            decides_automatically=self._current(attrs, "decides_automatically", False),
            automatic_decision_guard=self._current(
                attrs,
                "automatic_decision_guard",
                enums.AutomaticDecisionGuards.EARLIER_STEP,
            ),
            transition_mode=self._current(
                attrs, "transition_mode", enums.TransitionModes.AUTOMATIC_ON_COMPLETION
            ),
        )
        if "assigns_roles" in attrs:
            assigns_roles = attrs["assigns_roles"]
        elif self.instance is not None:
            assigns_roles = self.instance.assigns_roles.all()
        else:
            assigns_roles = []
        candidate.release_to_applicant = self._current(
            attrs, "release_to_applicant", False
        )
        candidate.collects_reviewer_suggestions = self._current(
            attrs, "collects_reviewer_suggestions", False
        )
        candidate.round_scoped = self._current(attrs, "round_scoped", False)
        candidate.min_score_threshold = self._current(attrs, "min_score_threshold")
        candidate.phase = self._current(attrs, "phase", WorkflowPhases.EVALUATION)
        candidate.project_end_offset_days = self._current(
            attrs, "project_end_offset_days"
        )
        candidate.allows_change_requests = self._current(
            attrs, "allows_change_requests", False
        )
        candidate.include_award_response = self._current(
            attrs, "include_award_response", False
        )
        if (
            self.instance is not None
            and "phase" in attrs
            and attrs["phase"] != self.instance.phase
            and self.instance.proposal_instances.exists()
        ):
            raise serializers.ValidationError(
                {
                    "phase": _(
                        "Proposals have walked this step; the phase it runs in "
                        "can no longer change. Add a new step instead."
                    )
                }
            )
        candidate.status_labels = self._current(attrs, "status_labels", {}) or {}
        candidate.applicant_visible = self._current(attrs, "applicant_visible", False)
        if self.instance is not None:
            self._refuse_applicant_rules_on_internal_step(attrs, candidate)
        if "input_steps" in attrs:
            input_steps = attrs["input_steps"]
        elif self.instance is not None and "order" in attrs:
            input_steps = list(self.instance.input_steps.all())
        else:
            input_steps = []
        try:
            candidate.clean()
            models.CallWorkflowStep.check_assigned_roles(
                kind.id, getattr(call, "pk", None), assigns_roles
            )
            models.CallWorkflowStep.check_input_steps(
                getattr(self.instance, "pk", None),
                getattr(call, "pk", None),
                self._current(attrs, "order"),
                input_steps,
                candidate.phase,
            )
            if "order" in attrs:
                models.CallWorkflowStep.check_reader_order(
                    getattr(self.instance, "pk", None), attrs["order"]
                )
            if call is not None:
                if "input_steps" in attrs or self.instance is None:
                    has_inputs = bool(input_steps)
                else:
                    has_inputs = self.instance.input_steps.exists()
                models.CallWorkflowStep.check_responses_decided(
                    call.pk,
                    changed={
                        "pk": getattr(self.instance, "pk", None),
                        "name": candidate.name,
                        "order": attrs.get("order"),
                        "is_enabled": candidate.is_enabled,
                        "kind": kind.id,
                        "outcome_set": candidate.outcome_set
                        or kind.default_outcome_set,
                        "has_inputs": has_inputs,
                    },
                )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "error_dict") else exc.messages
            )
        if "status_labels" in attrs:
            attrs["status_labels"] = candidate.status_labels

        guard_changed = "automatic_decision_guard" in attrs and (
            self.instance is None
            or attrs["automatic_decision_guard"]
            != self.instance.automatic_decision_guard
        )
        if (
            (attrs.get("decides_automatically") or guard_changed)
            and candidate.decides_automatically
            and candidate.is_enabled
            and isinstance(call, models.Call)
            and call.state == CallStates.ACTIVE
        ):
            self._refuse_unguarded_automatic_decision(call, attrs, candidate)

        # Checked on the changes that move steps, so a call already out of
        # order can still be edited into order.
        moves = self.instance is None or any(
            field in attrs
            for field in ("order", "is_enabled", "is_allocation_decision")
        )
        if (
            moves
            and call is not None
            and models.CallWorkflowStep.award_response_precedes_decision(
                call.pk,
                changed={
                    "pk": getattr(self.instance, "pk", None),
                    "key": self._current(attrs, "key"),
                    "order": attrs.get("order"),
                    "is_enabled": candidate.is_enabled,
                    "is_allocation_decision": is_allocation_decision,
                },
            )
        ):
            raise serializers.ValidationError({"order": AWARD_BEFORE_DECISION})

        return attrs

    # What makes a step internal to the call team (``is_internal_step``).
    INTERNAL_STEP_FIELDS = (
        "phase",
        "applicant_visible",
        "completer",
        "is_allocation_decision",
    )

    def _refuse_applicant_rules_on_internal_step(self, attrs, candidate):
        """Refuse an edit that makes the step internal while its notification
        rules still mail the applicant: the rules stay valid on their own
        but the call no longer is, and it cannot be exported, copied or
        replayed until they go."""
        changed = [field for field in self.INTERNAL_STEP_FIELDS if field in attrs]
        if not changed or not models.is_internal_step(candidate):
            return
        if any(
            models.is_applicant_recipient(rule.recipient, candidate)
            for rule in self.instance.notification_rules.all()
        ):
            raise serializers.ValidationError(
                {
                    changed[0]: _(
                        "This makes the step internal to the call team, and its "
                        "notification rules still mail the applicant: remove "
                        "those rules first."
                    )
                }
            )

    def _refuse_unguarded_automatic_decision(self, call, attrs, candidate):
        """On an active call, refuse an automatic decision nothing can stop:
        no earlier step can reject a proposal or, relying on the provider
        fulfilment, an accepted offering's orders would be fulfilled with
        nobody at the provider acting on them. Every proposal would award
        itself. A call in draft is checked when it is activated."""
        if candidate.relies_on_provider_fulfilment:
            refusal = utils.provider_fulfilment_refusal(call)
            if refusal:
                raise serializers.ValidationError({"automatic_decision_guard": refusal})
            return
        order = self._current(attrs, "order")
        earlier = models.CallWorkflowStep.objects.filter(call=call, is_enabled=True)
        if self.instance is not None:
            earlier = earlier.exclude(pk=self.instance.pk)
        if order is not None:
            earlier = earlier.filter(order__lt=order)
        if not any(step.can_end_negatively for step in earlier):
            raise serializers.ValidationError(
                {"decides_automatically": UNGUARDED_AUTOMATIC_DECISION}
            )

    def _refuse_conflicting_minimums(self):
        """Refuse min_reviewers and min_contributions saying different things.

        Both write the same field; the one read last would silently win.
        """
        data = getattr(self, "initial_data", None) or {}
        if "min_reviewers" not in data or "min_contributions" not in data:
            return
        values = {
            None if value in (None, "") else int(value)
            for value in (data["min_reviewers"], data["min_contributions"])
        }
        if len(values) > 1:
            raise serializers.ValidationError(
                {
                    "min_reviewers": _(
                        "min_reviewers is the former name of min_contributions; "
                        "send one of them, or the same value in both."
                    )
                }
            )

    @staticmethod
    def _has_proposals(call):
        return models.ProposalWorkflowStepInstance.objects.filter(
            call_step__call=call
        ).exists()

    # Fixed once proposals walk the call's steps.
    LOCKED_ONCE_WALKED = (
        "contributor_source",
        "per_requested_offering",
        "contributors_lead_only",
        "collects_reviewer_suggestions",
    )

    # Fixed while a proposal stands on the step.
    LOCKED_WHILE_ACTIVE = (
        "completer",
        "completer_proposal_role",
        "is_enabled",
        "round_scoped",
    )

    def _refuse_change_under_proposals(self, attrs):
        """Keep who completes a step, and whether it runs, while a proposal
        stands on it.

        The proposals at the step were handed to its completer: changing who
        that is would give their evaluation to someone else -- the applicant,
        say -- halfway through it, and disabling the step would leave them
        on a step that no longer runs.
        """
        instance = self.instance
        changed = [
            field
            for field in self.LOCKED_WHILE_ACTIVE
            if field in attrs and attrs[field] != getattr(instance, field)
        ]
        if not changed:
            return
        if not models.ProposalWorkflowStepInstance.objects.filter(
            call_step=instance, status=WorkflowStepInstanceStatuses.ACTIVE
        ).exists():
            return
        raise serializers.ValidationError(
            {
                field: _(
                    "Proposals stand on this step; who completes it and whether "
                    "it runs can no longer change until they leave it."
                )
                for field in changed
            }
        )

    def _refuse_form_change_once_reached(self, attrs):
        """Keep a step's form once proposals have reached the step.

        Answers are read through the step's current form: swapping or
        dropping it would hide what the step's contributors already answered,
        on the proposals at the step and on those past it. A step no proposal
        has reached yet takes another form freely.
        """
        instance = self.instance
        if "checklist" not in attrs or attrs["checklist"] == instance.checklist:
            return
        reached = models.ProposalWorkflowStepInstance.objects.filter(
            call_step=instance
        ).exclude(
            status__in=(
                WorkflowStepInstanceStatuses.PENDING,
                WorkflowStepInstanceStatuses.SKIPPED,
            )
        )
        if reached.exists():
            raise serializers.ValidationError(
                {
                    "checklist": _(
                        "Proposals have reached this step and its form holds "
                        "their answers; it can no longer change. Add a new step "
                        "with the other form instead."
                    )
                }
            )

    def _refuse_criteria_change_once_scored(self, attrs):
        """Keep a step's criteria and their weights once reviewers scored them.

        A review's scores are keyed by criterion, and its summary score was
        weighted by them: renaming, dropping or reweighting a criterion would
        orphan scores or leave summaries no longer what the criteria give.
        Reordering them changes neither.
        """
        if "criteria" not in attrs:
            return
        instance = self.instance
        current = {c.name: c.weight for c in instance.criteria.all()}
        # A criterion sent without a weight keeps the one it has.
        wanted = {
            c["name"]: c.get("weight", current.get(c["name"], 1))
            for c in attrs["criteria"]
        }
        if wanted == current:
            return
        if contributions.step_has_scored_reviews(instance):
            raise serializers.ValidationError(
                {
                    "criteria": _(
                        "Reviewers have scored this step's criteria: they can "
                        "be reordered, not changed."
                    )
                }
            )

    @extend_schema_field(serializers.BooleanField())
    def get_criteria_locked(self, obj) -> bool:
        if not isinstance(obj, models.CallWorkflowStep) or obj.pk is None:
            return False
        return contributions.step_has_scored_reviews(obj)

    def _refuse_contributor_change(self, attrs):
        """Keep who contributes, and how, once proposals walk the step.

        Their contributions were made from it; changing the source or the
        per-offering split would leave them counted against another rule.
        """
        instance = self.instance
        for field in (
            "contributor_source",
            "per_requested_offering",
            "contributors_lead_only",
        ):
            if field in attrs and attrs[field] != getattr(instance, field):
                raise serializers.ValidationError(
                    {
                        field: _(
                            "Proposals have been submitted to this call; who "
                            "contributes to its steps can no longer change."
                        )
                    }
                )
        field = "collects_reviewer_suggestions"
        if field in attrs and attrs[field] != getattr(instance, field):
            # Suggestions made through the step would lose their readers, or
            # a step would start collecting halfway through its proposals.
            raise serializers.ValidationError(
                {
                    field: _(
                        "Proposals have been submitted to this call; whether a "
                        "step collects reviewer suggestions can no longer change."
                    )
                }
            )

    def _validate_allocation_decision_change(self, attrs, call):
        """Keep the allocation decision where proposals and activation need it.

        Once a proposal has been submitted to the call, which step settles the
        award is part of the call's record: it cannot move to another step.
        And an active call cannot lose its allocation decision, by unflagging
        or disabling it.
        """
        instance = self.instance
        flag = attrs.get("is_allocation_decision", instance.is_allocation_decision)
        if (
            flag != instance.is_allocation_decision
            and models.ProposalWorkflowStepInstance.objects.filter(
                call_step__call=call
            ).exists()
        ):
            raise serializers.ValidationError(
                {
                    "is_allocation_decision": _(
                        "Proposals have been submitted to this call; the "
                        "allocation decision can no longer move to another step."
                    )
                }
            )
        enabled = attrs.get("is_enabled", instance.is_enabled)
        if (
            call.state == CallStates.ACTIVE
            and instance.is_allocation_decision
            and instance.is_enabled
            and not (flag and enabled)
        ):
            raise serializers.ValidationError(
                {
                    "is_allocation_decision": _(
                        "An active call needs its allocation decision step."
                    )
                }
            )

    @staticmethod
    def _lock_call(call):
        # Orders and keys are picked from the call's other steps; two writes
        # at once would pick the same one.
        models.Call.objects.select_for_update().filter(pk=call.pk).first()

    @transaction.atomic
    def create(self, validated_data):
        criteria = validated_data.pop("criteria", None)
        call = validated_data["call"]
        self._lock_call(call)
        order = validated_data.get("order")
        if order is None:
            validated_data["order"] = models.CallWorkflowStep.default_order_for(
                call.id, validated_data.get("key")
            )
        else:
            models.CallWorkflowStep.make_room(call.id, order)
        instance = super().create(validated_data)
        if criteria is not None:
            self._sync_criteria(instance, criteria)
        notification_rules.seed_default_rules(instance)
        if "include_award_response" in validated_data:
            self._sync_award_response_step(instance)
        self._check_roles_assigned_first(instance)
        return instance

    @staticmethod
    def _check_roles_assigned_first(instance):
        """Checked once the step is saved, its roles, order, stage and run
        condition with it: the transaction rolls the change back when it is
        refused."""
        try:
            models.CallWorkflowStep.check_roles_assigned_first(instance.call_id)
            models.CallWorkflowStep.check_stages_and_conditions(instance.call_id)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

    @transaction.atomic
    def update(self, instance, validated_data):
        criteria = validated_data.pop("criteria", None)
        self._lock_call(instance.call)
        order = validated_data.get("order")
        if order is not None and order != instance.order:
            models.CallWorkflowStep.make_room(
                instance.call_id, order, exclude_pk=instance.pk
            )
        instance = super().update(instance, validated_data)
        if criteria is not None:
            self._sync_criteria(instance, criteria)
        if "include_award_response" in validated_data:
            self._sync_award_response_step(instance)
        self._check_roles_assigned_first(instance)
        return instance

    def _sync_criteria(self, instance, criteria):
        names = [c["name"] for c in criteria]
        instance.criteria.exclude(name__in=names).delete()
        for entry in criteria:
            models.WorkflowCriterion.objects.update_or_create(
                workflow_step=instance,
                name=entry["name"],
                defaults={
                    "order": entry.get("order", 0),
                    # Left out, a criterion keeps its weight (1 when new).
                    **({"weight": entry["weight"]} if "weight" in entry else {}),
                },
            )

    def _sync_award_response_step(self, instance):
        """Add, re-enable or disable the award response after the decision.

        ``include_award_response`` predates steps of a kind: it stands for an
        applicant step, right after the allocation decision, whose decline
        cancels the proposal. Kept for one release; the step is now added by
        its key like any other.
        """
        if not instance.is_allocation_decision:
            return
        award_step = models.CallWorkflowStep.objects.filter(
            call=instance.call, key=AWARD_RESPONSE_KEY
        ).first()
        if not instance.include_award_response:
            if award_step is not None and award_step.is_enabled:
                award_step.is_enabled = False
                award_step.save(update_fields=["is_enabled"])
            return
        if award_step is not None:
            if not award_step.is_enabled:
                award_step.is_enabled = True
                award_step.save(update_fields=["is_enabled"])
            return
        award_step = models.CallWorkflowStep.create_default(
            instance.call, AWARD_RESPONSE_KEY, order=instance.order + 1
        )
        notification_rules.seed_default_rules(award_step)


class StepOutcomeSetSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    outcomes = serializers.ListField(child=serializers.CharField())


class StepKindNotificationRuleSerializer(serializers.Serializer):
    trigger = serializers.CharField()
    recipient = serializers.CharField()
    days_before = serializers.IntegerField(allow_null=True)


class StepKindSerializer(serializers.Serializer):
    """A kind call steps are made of, for the workflow configuration UI."""

    id = serializers.ChoiceField(choices=StepKinds.CHOICES)
    name = serializers.CharField()
    description = serializers.CharField()
    outcome_sets = serializers.SerializerMethodField()
    default_outcome_set = serializers.CharField()
    default_completer = serializers.CharField()
    config_fields = serializers.SerializerMethodField()
    default_notification_rules = serializers.SerializerMethodField()
    applicant_facing = serializers.BooleanField()

    @extend_schema_field(StepOutcomeSetSerializer(many=True))
    def get_outcome_sets(self, kind):
        names = dict(enums.OutcomeSets.CHOICES)
        return [
            {
                "id": outcome_set,
                "name": names[outcome_set],
                "outcomes": list(enums.OutcomeSets.OUTCOMES[outcome_set]),
            }
            for outcome_set in kind.outcome_sets
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_config_fields(self, kind):
        """Call step fields that configure a step of this kind."""
        return list(COMMON_STEP_FIELDS) + list(kind.config_fields)

    @extend_schema_field(StepKindNotificationRuleSerializer(many=True))
    def get_default_notification_rules(self, kind):
        return [
            {"trigger": trigger, "recipient": recipient, "days_before": days_before}
            for trigger, recipient, days_before in (
                *enums.COMMON_NOTIFICATION_RULES,
                *kind.default_notification_rules,
            )
        ]


class StepChecklistStatusSerializer(serializers.Serializer):
    """Compact per-step checklist status surfaced on workflow_states so the UI
    can show a badge and gate the Complete button."""

    has_checklist = serializers.BooleanField()
    checklist_required = serializers.BooleanField()
    checklist_name = serializers.CharField(allow_null=True)
    checklist_completed = serializers.BooleanField()
    unanswered_required_count = serializers.IntegerField()
    can_read = serializers.BooleanField(
        help_text=(
            "Whether the caller may read the step's form as a whole "
            "(step-checklist without a contribution): its responsible role, "
            "the call team, staff, or someone who may contribute to it. A "
            "contributor's own card is read through its contribution."
        )
    )


class StepScoreGateSerializer(serializers.Serializer):
    score = serializers.FloatField(
        allow_null=True,
        help_text="The score the threshold is checked against; null while there is none.",
    )
    threshold = serializers.FloatField(help_text="The step's minimum score.")
    source_step_name = serializers.CharField(
        allow_null=True,
        help_text=(
            "The input step the score comes from; null when it is the "
            "average of the proposal's (or this step's) reviews."
        ),
    )
    passes = serializers.BooleanField(
        help_text="Whether the step may be completed with an outcome other than declined."
    )


class ProposalWorkflowStepInstanceSerializer(serializers.ModelSerializer):
    call_step_uuid = serializers.UUIDField(
        source="call_step.uuid", read_only=True, format="hex"
    )
    kind = serializers.ChoiceField(
        source="call_step.kind", choices=StepKinds.CHOICES, read_only=True
    )
    outcome_set = serializers.ReadOnlyField(source="call_step.outcome_set")
    allowed_outcomes = serializers.ListField(
        source="call_step.allowed_outcomes",
        child=serializers.CharField(),
        read_only=True,
    )
    contributor_source = serializers.CharField(
        source="call_step.contributor_source", read_only=True
    )
    contributor_proposal_role_uuid = serializers.SerializerMethodField(
        help_text=(
            "The evaluator role whose holders contribute to the step, when its "
            "contributors hold one."
        )
    )
    per_requested_offering = serializers.BooleanField(
        source="call_step.per_requested_offering", read_only=True
    )
    contributors_lead_only = serializers.BooleanField(
        source="call_step.contributors_lead_only",
        read_only=True,
        help_text="Whether only the lead of the evaluator role contributes.",
    )
    delegable_to = serializers.CharField(
        source="call_step.delegable_to",
        read_only=True,
        help_text="Who a contribution slot of the step can be handed to.",
    )
    round_scoped = serializers.BooleanField(
        source="call_step.round_scoped",
        read_only=True,
        help_text=(
            "Whether the step runs once per round: it is completed for the "
            "round, never on the proposal alone."
        ),
    )
    awaiting_lead = serializers.SerializerMethodField(
        help_text=(
            "Why the active step waits for the lead of its evaluator role, "
            "when only the lead contributes and none can; null otherwise, "
            "and for readers outside the call team."
        )
    )
    completion_warning = serializers.SerializerMethodField(
        help_text=(
            "What completing the active step now leaves short, for the "
            "completer to confirm: a later review step needing more reviews "
            "than reviewers are assigned. Null otherwise, and for readers "
            "outside the call team."
        )
    )
    waiting_for = serializers.SerializerMethodField(
        help_text=(
            "Why the active step cannot move on without someone being added "
            "or a call manager acting in another's place, with the way "
            "forward; null when its own people can move it, and for readers "
            "outside the call team."
        )
    )
    on_negative = serializers.ChoiceField(
        source="call_step.on_negative",
        choices=OnNegativeOutcomes.CHOICES,
        read_only=True,
    )
    contribution_outcomes = serializers.ListField(
        source="call_step.contribution_outcomes",
        child=serializers.CharField(),
        read_only=True,
        help_text=(
            "Outcomes a contribution to the step may conclude with; empty when "
            "contributions carry no outcome of their own."
        ),
    )
    step_name = serializers.SerializerMethodField()
    step_description = serializers.SerializerMethodField()
    completer = serializers.SerializerMethodField()
    completer_proposal_role_uuid = serializers.UUIDField(
        source="call_step.completer_proposal_role.uuid",
        read_only=True,
        allow_null=True,
        format="hex",
        help_text="The evaluator role whose holders complete the step, if any.",
    )
    completer_proposal_role_name = serializers.ReadOnlyField(
        source="call_step.completer_proposal_role.name", allow_null=True
    )
    responsible_role = serializers.SerializerMethodField()
    completed_by = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    applicant_visible = serializers.SerializerMethodField()
    duration_in_days = serializers.SerializerMethodField()
    is_required = serializers.SerializerMethodField()
    rejection_reason = serializers.SerializerMethodField()
    # Declared as a SerializerMethodField (rather than letting ModelSerializer
    # auto-generate it from the model field) so the schema can mark it as
    # nullable: the response is asymmetric — call-management team gets the
    # text, everyone else gets null. Without allow_null=True the generated
    # SDK types it as a required string and frontends crash on the applicant
    # view (CLAUDE.md "Nullable FKs MUST use allow_null=True").
    internal_notes = serializers.SerializerMethodField()
    checklist_status = serializers.SerializerMethodField()
    input_step_uuids = serializers.SerializerMethodField()
    release_to_applicant = serializers.BooleanField(
        source="call_step.release_to_applicant",
        read_only=True,
        help_text="Whether the step's report is released to the applicant.",
    )
    released_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="When the step's report was released to the applicant.",
    )
    collects_reviewer_suggestions = serializers.BooleanField(
        source="call_step.collects_reviewer_suggestions",
        read_only=True,
        help_text=(
            "Whether the step's contributors suggest reviewers for the proposal."
        ),
    )
    allows_change_requests = serializers.BooleanField(
        source="call_step.allows_change_requests",
        read_only=True,
        help_text=(
            "Whether whoever acts on the step may send the proposal back to "
            "its applicant for changes."
        ),
    )
    can_release = serializers.SerializerMethodField(
        help_text=(
            "Whether the caller may release the step's report to the "
            "applicant now: its completer or those overseeing the call, once "
            "the step is completed and not yet released."
        )
    )
    can_suggest_reviewers = serializers.SerializerMethodField(
        help_text=(
            "Whether the caller may suggest a reviewer through the step now: "
            "it is the active step collecting suggestions and the caller's "
            "own contribution to it is still open."
        )
    )
    ready_to_complete = serializers.SerializerMethodField(
        help_text=(
            "For whoever may complete the active step: every contribution "
            "is in and its completion gates are met, so it waits only for "
            "them. False for everyone else."
        )
    )
    decided_automatically = serializers.SerializerMethodField(
        help_text=(
            "Whether the system took this allocation decision itself, on a "
            "call whose decision decides automatically."
        )
    )
    score_gate = serializers.SerializerMethodField(
        help_text=(
            "The active step's score against its minimum score threshold, "
            "before anyone decides: below it, only declining is possible. "
            "Null without a threshold, once the step is no longer active, "
            "and for readers outside the call team."
        )
    )
    stage = serializers.SerializerMethodField(
        help_text=(
            "The stage the step runs in on this proposal: steps of one stage "
            "run at the same time, and the next stage starts once all of "
            "them have ended."
        )
    )
    run_condition = RunConditionField(
        read_only=True,
        help_text=(
            "When the step runs on this proposal, fixed when it was "
            "submitted; empty when it runs on every proposal."
        ),
    )
    status_label = serializers.SerializerMethodField(
        help_text=(
            "What the call configured the caller's dashboard to call the step "
            "in its state: the applicant team's labels on a step visible to "
            "them and on the allocation decision, else the evaluators'. Null "
            "to fall back to the computed label."
        )
    )
    skipped_reason = serializers.SerializerMethodField(
        help_text=(
            "Why a skipped step did not run: 'condition_not_met' when its "
            "run condition did not hold. Null otherwise."
        )
    )

    @extend_schema_field(serializers.IntegerField())
    def get_stage(self, obj):
        return obj.stage_key

    run_uuid = serializers.UUIDField(
        source="run.uuid",
        read_only=True,
        allow_null=True,
        format="hex",
        help_text=(
            "The extension request or final report the step belongs to; null "
            "for a step of the evaluation."
        ),
    )
    phase = serializers.ChoiceField(
        source="call_step.phase", choices=WorkflowPhases.CHOICES, read_only=True
    )
    is_overdue = serializers.SerializerMethodField(
        help_text=(
            "A final report past its due date, still to be submitted. Other "
            "steps lapse at their deadline instead."
        )
    )

    def get_is_overdue(self, obj) -> bool:
        return bool(
            obj.run_id
            and obj.call_step.phase == WorkflowPhases.CLOSING
            and obj.call_step.kind == StepKinds.APPLICANT_ACTION
            and obj.status == WorkflowStepInstanceStatuses.ACTIVE
            and obj.deadline is not None
            and obj.deadline < timezone.now()
        )

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_status_label(self, obj):
        # Picked in to_representation, from what the reader is shown.
        return None

    @extend_schema_field(
        serializers.ChoiceField(choices=["condition_not_met"], allow_null=True)
    )
    def get_skipped_reason(self, obj):
        if (
            obj.status == WorkflowStepInstanceStatuses.SKIPPED
            and obj.run_condition
            and obj.completed_at is not None
        ):
            return "condition_not_met"
        return None

    class Meta:
        model = models.ProposalWorkflowStepInstance
        fields = [
            "uuid",
            "step",
            "call_step_uuid",
            "kind",
            "order",
            "stage",
            "run_uuid",
            "phase",
            "is_overdue",
            "run_condition",
            "skipped_reason",
            "status_label",
            "outcome_set",
            "allowed_outcomes",
            "contributor_source",
            "contributor_proposal_role_uuid",
            "per_requested_offering",
            "round_scoped",
            "contributors_lead_only",
            "delegable_to",
            "awaiting_lead",
            "waiting_for",
            "completion_warning",
            "on_negative",
            "contribution_outcomes",
            "is_allocation_decision",
            "step_name",
            "step_description",
            "completer",
            "responsible_role",
            "completer_proposal_role_uuid",
            "completer_proposal_role_name",
            "status",
            "outcome",
            "outcome_reason",
            "rejection_reason",
            "internal_notes",
            "started_at",
            "completed_at",
            "completed_by",
            "deadline",
            "applicant_visible",
            "duration_in_days",
            "is_required",
            "checklist_status",
            "input_step_uuids",
            "release_to_applicant",
            "released_at",
            "can_release",
            "collects_reviewer_suggestions",
            "allows_change_requests",
            "can_suggest_reviewers",
            "ready_to_complete",
            "score_gate",
            "decided_automatically",
        ]
        read_only_fields = fields

    # Steps whose outcome/commentary is peer-review content, gated by the
    # call's reviews_visible_to_submitters setting (separate from the reviewer
    # *identity* gate that governs completed_by): steps collecting reviews,
    # and approval decisions other than the allocation decision -- what the
    # expert and panel reviews were. Read from the kind and outcome set, which
    # are fixed per step, not from who completes it, which the call manager
    # may change. Eligibility and feasibility outcomes stay visible.
    @staticmethod
    def _is_review_step(instance):
        call_step = instance.call_step
        if call_step.outcome_set == OutcomeSets.REVIEWED:
            return True
        # The decision closing an extension request or the final report is
        # the applicant team's answer, not a review of their proposal.
        return (
            call_step.kind == StepKinds.DECISION
            and call_step.outcome_set == OutcomeSets.APPROVAL
            and not instance.is_allocation_decision
            and call_step.phase not in WorkflowPhases.POST_AWARD
        )

    def _context_flag(self, name, instance) -> bool:
        """A visibility flag the view set: a bool for every step, or a
        callable deciding it per step instance."""
        flag = self.context.get(name)
        if callable(flag):
            return bool(flag(instance))
        return bool(flag)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # ``completed_by`` reveals the identity of whoever completed the step —
        # including the reviewer on expert_review and the panel member on
        # panel_review. Honour the call's blind-review setting: null it for
        # proposal submitters (and any other non-call-team viewer) unless the
        # call reveals reviewer identity to submitters. ``can_view_step_actors``
        # is set by the view; defaulting to hidden fails closed if a future
        # caller forgets to set it. Mirrors the Review serializer's
        # reviewer_identity_visible_to_submitters gate.
        if not self._context_flag("can_view_step_actors", instance):
            data["completed_by"] = None
        elif data["completed_by"] is not None and self._completed_by_applicant(
            instance
        ):
            # The step actor gate admits the whole call team, evaluators
            # included; an applicant-side completer (award_response, or the
            # author wherever they acted) is applicant identity, which an
            # evaluator sees only if the call exposes it.
            concealed = self.context.get("concealed_applicant_attributes") or ()
            if "username" in concealed:
                data["completed_by"] = None
        if data["completed_by"] is not None and self._conceals_blind_completer(
            instance
        ):
            data["completed_by"] = None
        # A step the call keeps from the applicant (``applicant_visible`` off)
        # is an internal check on the applicant's side of the proposal, and
        # the applicant's step list says no more than that. The applicant
        # team learns neither its verdict, its reasoning nor who gave them.
        # Two outcomes stay: the allocation decision, which the held-decision
        # rule below governs, and a negative outcome that ended the proposal,
        # which is the reason the applicant's proposal stopped. A call that
        # shows its reviews or reviewers to submitters keeps showing them on
        # the steps collecting them.
        if self._hides_internal_step(instance):
            call = instance.proposal.round.call
            review_step = self._is_review_step(instance)
            if not (review_step and call.reviews_visible_to_submitters):
                data["outcome"] = None
                data["outcome_reason"] = ""
                data["rejection_reason"] = None
            if not (review_step and call.reviewer_identity_visible_to_submitters):
                data["completed_by"] = None
            # Nor which outcome of which step it waits on: that names the
            # call's internal steps and what they may conclude.
            data["run_condition"] = {}
            data["skipped_reason"] = None
        # Hide the reviewer/panel verdict and free-text commentary on the peer
        # review steps from submitters when the call keeps reviews private. This
        # must strip *every* field that surfaces the reviewer's words:
        # ``outcome`` (the verdict), ``outcome_reason`` (the raw commentary) and
        # ``rejection_reason`` — which is just ``outcome_reason`` re-exposed via a
        # SerializerMethodField and would otherwise leak the exact rejection text
        # the mask exists to hide. The applicant still learns the *decision* from
        # the proposal's own state and the call manager's allocation comment;
        # what stays private here is the reviewer's step-level reasoning.
        # A decision held for the round's publication is still being made,
        # as far as the applicant side can tell.
        if instance.is_allocation_decision and self.context.get("decision_withheld"):
            data["decided_automatically"] = False
            data["status"] = WorkflowStepInstanceStatuses.ACTIVE
            data["outcome"] = None
            data["outcome_reason"] = ""
            data["rejection_reason"] = None
            data["completed_at"] = None
            data["completed_by"] = None
        # The view leaves the flag out for readers it does not narrow.
        if "can_view_step_outcome" in self.context and not self._context_flag(
            "can_view_step_outcome", instance
        ):
            data["outcome"] = None
            data["outcome_reason"] = ""
            data["rejection_reason"] = None
        if self._is_review_step(instance) and not self._context_flag(
            "can_view_review_content", instance
        ):
            data["outcome"] = None
            data["outcome_reason"] = ""
            data["rejection_reason"] = None
        # The feedback given on completing a step whose report is released
        # is the call team's until the release, then also the applicant
        # team's -- whom it was written for -- and the step's own holders'.
        # Other evaluators and the providers never read it.
        if (
            instance.feedback_withheld
            and not self._context_flag("can_view_internal_notes", instance)
            and not (
                instance.released_at is not None
                and self._reads_released_feedback(instance)
            )
        ):
            data["outcome_reason"] = ""
            data["rejection_reason"] = None
        request = self.context.get("request")
        if request is not None and self.conceals_offering_step_reason_from(
            request.user, instance, contributions.access_for(request)
        ):
            data["outcome_reason"] = ""
            data["rejection_reason"] = None
        # Last: picked from what the reader is shown, so a masked outcome
        # or a held decision never picks its outcome's label.
        data["status_label"] = self._status_label(instance, data)
        return data

    @staticmethod
    def conceals_offering_step_reason_from(user, instance, access=None) -> bool:
        """Whether the reason given for a per-offering step's outcome is kept
        from ``user``: a provider reading the proposal for their own
        offering only (``permissions.offering_scope``). The reason speaks to
        every offering's slot; their own slot's outcome and comment are
        theirs on the slot. Mail about the step tells them no more."""
        call_step = instance.call_step
        if call_step is None or not call_step.per_requested_offering:
            return False
        access = access or contributions.Access(user)
        return access.offering_scope(instance.proposal) is not None

    def _hides_internal_step(self, instance) -> bool:
        request = self.context.get("request")
        if request is None:
            return False
        return self.hides_internal_step_from(
            request.user,
            instance,
            access=contributions.access_for(request),
            readers=self.context.setdefault("_reads_from_outside", {}),
        )

    @classmethod
    def hides_internal_step_from(
        cls, user, instance, access=None, readers=None
    ) -> bool:
        """Whether ``instance`` is a step the call keeps from ``user``, who
        reads it from the applicant's side: its verdict, reasoning and
        completer are not theirs (see ``to_representation``). Mail about
        the step tells them no more.

        ``readers`` caches, per proposal, which side ``user`` reads it from.
        """
        call_step = instance.call_step
        if call_step is None or call_step.applicant_visible:
            return False
        if instance.is_allocation_decision or cls._ended_proposal(instance):
            return False
        # Nor the decision on the team's own extension request or final
        # report: its outcome is theirs to learn, as its labels say.
        if step_status_labels.applies_to_applicant(call_step):
            return False
        # A report released to the applicant is theirs to read; the released
        # feedback rule below governs it.
        if instance.released_at is not None:
            return False
        access = access or contributions.Access(user)
        reader = cls.reader_side(user, instance.proposal, access, readers)
        if reader == "outside":
            # Their own step's outcome is theirs.
            return not access.holds_step_work(instance)
        return reader == "applicant"

    @staticmethod
    def reader_side(user, proposal, access, readers=None):
        """Which side ``user`` reads ``proposal``'s steps from: "applicant",
        "outside", or None for the call team and the evaluators.

        ``readers`` caches the answer per proposal.
        """
        readers = {} if readers is None else readers
        if proposal.pk not in readers:
            # The applicant team, and whoever reads the proposal with no part
            # in its evaluation -- a provider assessing their own offering --
            # read it from the applicant's side. The call team, and anyone
            # holding a role on the call or an evaluator role on the proposal,
            # read it under the rules for evaluators.
            if step_inputs.reads_as_call_team(user, proposal):
                readers[proposal.pk] = None
            elif access.on_applicant_team(proposal):
                readers[proposal.pk] = "applicant"
            elif permissions_utils.has_user(
                proposal.round.call, user
            ) or proposal_roles.holds_role(user, proposal):
                readers[proposal.pk] = None
            else:
                readers[proposal.pk] = "outside"
        return readers[proposal.pk]

    @classmethod
    def label_audience(cls, user, instance, access=None, readers=None, side=None):
        """Whose status labels ``user`` reads for the step ``instance``: one
        of ``status_labels.AUDIENCES``, or None for none at all.

        The one place readers are sorted into the labels' audiences:

        - the applicant team reads the applicant's;
        - the call team -- staff, support, and whoever oversees the call or
          holds a call-wide role on it (``step_inputs.reads_as_call_team``)
          -- reads the call team's;
        - an offering team reads the offering team's on a step it works on:
          a provider reading from outside the evaluation, on the steps they
          hold work on, and anyone on the team of an offering with a slot
          on a per-offering step;
        - every other evaluator reads the evaluators'.

        A provider reading from outside the evaluation reads no label of a
        step they do not work on. ``side`` is ``reader_side``'s answer when
        the caller has it, or the proposal list's (``"call_team"``,
        ``"evaluator"``).
        """
        access = access or contributions.Access(user)
        proposal = instance.proposal
        if side is None:
            side = cls.reader_side(user, proposal, access, readers)
        if side == "applicant":
            return step_status_labels.APPLICANT
        if side == "outside":
            if access.holds_step_work(instance):
                return step_status_labels.OFFERING_TEAM
            return None
        if user.is_staff or user.is_support:
            return step_status_labels.CALL_TEAM
        if side == "call_team" or (
            side != "evaluator" and step_inputs.reads_as_call_team(user, proposal)
        ):
            return step_status_labels.CALL_TEAM
        if instance.call_step.per_requested_offering and (
            contributions.assesses_a_slot_of(access, instance)
        ):
            return step_status_labels.OFFERING_TEAM
        return step_status_labels.EVALUATORS

    @staticmethod
    def step_counts(instance, cache=None):
        """The counts a label of ``instance`` may show, worked out once per
        step and kept in ``cache``."""
        cache = {} if cache is None else cache
        if instance.pk not in cache:
            cache[instance.pk] = contributions.step_counts(instance)
        return cache[instance.pk]

    def _status_label(self, instance, data):
        """The label the reader's dashboard gives the step, picked from what
        they are shown of it (``data``), or None for the computed one."""
        call_step = instance.call_step
        if call_step is None or not call_step.status_labels:
            return None
        request = self.context.get("request")
        if request is None:
            # Served without a request only to the call's managers.
            audience = step_status_labels.CALL_TEAM
        else:
            audience = self.label_audience(
                request.user,
                instance,
                contributions.access_for(request),
                self.context.setdefault("_reads_from_outside", {}),
            )
            if audience is None:
                return None
        label = step_status_labels.pick(
            step_status_labels.configured(call_step, audience),
            data["status"],
            data["outcome"],
            changes_requested=self._changes_requested(instance, audience),
        )
        return step_status_labels.render(
            label,
            lambda: self.step_counts(
                instance, self.context.setdefault("_step_counts", {})
            ),
        )

    def _changes_requested(self, instance, audience) -> bool:
        """Whether the step has sent the proposal back to its applicant, for
        a reader told of it: the applicant team and those overseeing the
        call (as ``ProposalSerializer.open_change_requests``)."""
        if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
            return False
        # In review, or accepted with the request made in a post-award run.
        if instance.proposal.state not in (
            ProposalStates.IN_REVIEW,
            ProposalStates.ACCEPTED,
        ):
            return False
        request = self.context.get("request")
        if request is not None and audience != step_status_labels.APPLICANT:
            user = request.user
            if not (
                user.is_staff
                or user.is_support
                or proposal_permissions.oversees_proposal_call(user, instance.proposal)
            ):
                return False
        cache = self.context.setdefault("_changes_requested_steps", {})
        if instance.proposal_id not in cache:
            cache[instance.proposal_id] = set(
                change_requests.open_requests()
                .filter(
                    proposal_id=instance.proposal_id,
                    contribution__isnull=True,
                    step_instance__status=WorkflowStepInstanceStatuses.ACTIVE,
                )
                .values_list("step_instance_id", flat=True)
            )
        return instance.pk in cache[instance.proposal_id]

    @staticmethod
    def _ended_proposal(instance) -> bool:
        if instance.outcome == WorkflowStepOutcomes.REJECTED:
            return True
        return (
            instance.outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
            and instance.call_step.on_negative != OnNegativeOutcomes.CONTINUE
        )

    def _reads_released_feedback(self, instance) -> bool:
        request = self.context.get("request")
        if request is None:
            return False
        access = contributions.access_for(request)
        return access.on_applicant_team(instance.proposal) or access.holds_step_work(
            instance
        )

    def _conceals_blind_completer(self, instance) -> bool:
        """Whether who completed a blind step is kept from the reader.

        A step its contributions complete is completed by whoever submitted
        the last of them, and a contributor may complete one by hand: either
        way the completer names a contributor the blind step keeps from its
        readers. Only those reading every contribution of the step, and the
        completer themselves, learn who it was. The applicant team reads it
        under the call's reviewer identity setting, as it reads the
        contributors themselves (``contributions.Access.conceals_contributor``).
        """
        call_step = instance.call_step
        if call_step is None or not call_step.blind_review:
            return False
        request = self.context.get("request")
        if request is None:
            return True
        if instance.completed_by_id == request.user.id:
            return False
        access = contributions.access_for(request)
        if access.reads_all(instance):
            return False
        return not access.on_applicant_team(instance.proposal)

    @extend_schema_field(serializers.BooleanField())
    def get_decided_automatically(self, obj) -> bool:
        if not (
            obj.is_allocation_decision
            and obj.status == WorkflowStepInstanceStatuses.COMPLETED
            and obj.completed_by_id is not None
        ):
            return False
        cache = self.context.setdefault("_system_robot_id", [])
        if not cache:
            cache.append(
                core_models.User.all_objects.filter(username="system_robot")
                .values_list("id", flat=True)
                .first()
            )
        return obj.completed_by_id == cache[0]

    def _completed_by_applicant(self, obj) -> bool:
        # The author, and whoever is on the applicant team now.
        if obj.completed_by_id == obj.proposal.created_by_id:
            return True
        team = self.context.setdefault("_applicant_team_user_ids", {})
        if obj.proposal_id not in team:
            team[obj.proposal_id] = proposal_roles.applicant_team_user_ids(obj.proposal)
        if obj.completed_by_id in team[obj.proposal_id]:
            return True
        return self.get_completer(obj) == ResponsibleRoles.APPLICANT

    @extend_schema_field(serializers.UUIDField(format="hex", allow_null=True))
    def get_contributor_proposal_role_uuid(self, obj):
        call_step = obj.call_step
        try:
            kind, _ = enums.parse_contributor_source(call_step.contributor_source)
        except ValueError:
            return None
        if kind != ContributorSources.PROPOSAL_ROLE:
            return None
        # Steps of one call name the same few roles; read each once.
        cache = self.context.setdefault("_contributor_roles", {})
        key = (call_step.call_id, call_step.contributor_source)
        if key not in cache:
            role = proposal_roles.resolve_role(
                call_step.call, call_step.contributor_source
            )
            cache[key] = role.uuid.hex if role is not None else None
        return cache[key]

    # Why a step waits, and what completing it leaves short, tell how the
    # evaluation is staffed and who among the evaluators has a conflict of
    # interest: the call team's side of the step, as its internal notes are,
    # never the applicant's.

    @extend_schema_field(StepScoreGateSerializer(allow_null=True))
    def get_score_gate(self, obj):
        # The score sums up the evaluators' grading: the call team's side.
        if (
            obj.status != WorkflowStepInstanceStatuses.ACTIVE
            or obj.call_step.min_score_threshold is None
            or not self._context_flag("can_view_internal_notes", obj)
        ):
            return None
        gate = workflow_service.score_gate(obj)
        return StepScoreGateSerializer(
            {**gate, "threshold": float(gate["threshold"])}
        ).data

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_completion_warning(self, obj):
        if not self._context_flag("can_view_internal_notes", obj):
            return None
        return contributions.reviewers_short_ahead(obj)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_waiting_for(self, obj):
        if obj.status != WorkflowStepInstanceStatuses.ACTIVE:
            return None
        if not self._context_flag("can_view_internal_notes", obj):
            return None
        return contributions.why_step_waits(obj)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_awaiting_lead(self, obj):
        if (
            not obj.call_step.contributors_lead_only
            or obj.status != WorkflowStepInstanceStatuses.ACTIVE
        ):
            return None
        if not self._context_flag("can_view_internal_notes", obj):
            return None
        return proposal_roles.missing_lead(obj.proposal, obj.call_step)

    @extend_schema_field(serializers.CharField())
    def get_step_name(self, obj):
        return obj.call_step.name or obj.step

    @extend_schema_field(serializers.CharField())
    def get_step_description(self, obj):
        return obj.call_step.description

    @extend_schema_field(StepChecklistStatusSerializer(allow_null=True))
    def get_checklist_status(self, obj):
        call_step = obj.call_step
        if not call_step.checklist_id:
            return None
        unanswered = contributions.unanswered_required_questions(obj).count()
        request = self.context.get("request")
        user = getattr(request, "user", None)
        # The step-checklist endpoint's own rule, so the page does not ask
        # for a form it will be refused.
        can_read = bool(
            user is not None
            and user.is_authenticated
            and (
                proposal_permissions.user_can_read_step_checklist(
                    user, obj.proposal, call_step
                )
                or contributions.may_contribute(user, obj)
            )
        )
        return {
            "has_checklist": True,
            "can_read": can_read,
            "checklist_required": call_step.checklist_required,
            "checklist_name": call_step.checklist.name,
            "checklist_completed": unanswered == 0,
            "unanswered_required_count": unanswered,
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_completer(self, obj):
        return obj.call_step.completer

    @extend_schema_field(serializers.BooleanField())
    def get_can_release(self, obj) -> bool:
        request = self.context.get("request")
        if (
            request is None
            or not obj.call_step.release_to_applicant
            or obj.status != WorkflowStepInstanceStatuses.COMPLETED
            or obj.released_at is not None
        ):
            return False
        return step_inputs.can_release(request.user, obj)

    def get_can_suggest_reviewers(self, obj) -> bool:
        request = self.context.get("request")
        if (
            request is None
            or not obj.call_step.collects_reviewer_suggestions
            or obj.status != WorkflowStepInstanceStatuses.ACTIVE
        ):
            return False
        return reviewer_suggestions.may_suggest(request.user, obj)

    def get_ready_to_complete(self, obj) -> bool:
        request = self.context.get("request")
        if (
            request is None
            or obj.status != WorkflowStepInstanceStatuses.ACTIVE
            or not obj.call_step.collects_contributions
        ):
            return False
        return workflow_service.is_ready_to_complete(request.user, obj)

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_input_step_uuids(self, obj):
        """The call steps whose outputs this step reads on the proposal."""
        return [step.uuid.hex for step in obj.input_steps.all()]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_responsible_role(self, obj):
        # Deprecated alias of completer, kept for one release.
        return self.get_completer(obj)

    @extend_schema_field(serializers.BooleanField())
    def get_applicant_visible(self, obj):
        return obj.call_step.applicant_visible

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_duration_in_days(self, obj):
        return obj.call_step.duration_in_days

    @extend_schema_field(serializers.BooleanField())
    def get_is_required(self, obj):
        # The allocation decision is the one step a call cannot do without.
        return obj.is_allocation_decision

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_rejection_reason(self, obj):
        # Sugar: ``outcome_reason`` is the underlying field, but only carries
        # rejection text when ``outcome`` is ``rejected``. The frontend
        # otherwise has to know that convention; this surfaces it cleanly.
        #
        # Return the raw value (which is ``""`` when blank, not None) so a
        # rejected step with an empty reason is still distinguishable from a
        # non-rejected step. Frontends should branch on ``=== null`` to detect
        # "not rejected" rather than truthiness on the string.
        if obj.outcome == WorkflowStepOutcomes.REJECTED:
            return obj.outcome_reason
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_internal_notes(self, obj):
        # ``can_view_internal_notes`` is set by the view after the same
        # permission check used to gate write access. Returning None (not
        # absent) keeps the response shape stable so SDK consumers can type
        # the field as Optional[str] instead of "sometimes present".
        if not self._context_flag("can_view_internal_notes", obj):
            return None
        return obj.internal_notes or None


class StepInputSerializer(serializers.Serializer):
    """What one input step of a proposal's workflow step produced, as the
    caller may read it."""

    step = ProposalWorkflowStepInstanceSerializer(
        help_text="The input step on the proposal, its outcome masked as on "
        "the proposal's step list."
    )
    contributions = StepContributionWithAnswersSerializer(
        many=True,
        help_text="The step's contributions with their answers.",
    )
    reviews = ProposalReviewSerializer(
        many=True,
        help_text=(
            "The submitted reviews of a step collecting reviews; for the "
            "applicant without private comments, and without the reviewer "
            "unless the call reveals reviewers to applicants."
        ),
    )


class ReleaseStepReportSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        help_text="UUID of the call step whose report is released."
    )
    text = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "The text the applicant reads in place of the step's report, such "
            "as the report edited by the call manager. The report itself when "
            "left out."
        ),
    )


class ReleasedReportSerializer(serializers.Serializer):
    """A step's report released to the applicant."""

    call_step_uuid = serializers.UUIDField(format="hex")
    step_name = serializers.CharField()
    released_at = serializers.DateTimeField()
    released_by_name = serializers.CharField(
        allow_null=True,
        help_text="Who released it; for the call team only.",
    )
    text = serializers.CharField(
        allow_blank=True,
        help_text="The text released in place of the report; empty when the "
        "report itself was released.",
    )
    contributions = StepContributionWithAnswersSerializer(
        many=True,
        help_text=(
            "The report: the step's submitted contributions with their "
            "answers. For the applicant, empty when a text was released in "
            "its place."
        ),
    )


class CompleteWorkflowStepSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=True,
        help_text=(
            "UUID of the workflow step instance the client believes is active. "
            "Used to detect concurrent step transitions."
        ),
    )
    outcome = serializers.ChoiceField(
        choices=WorkflowStepOutcomes.CHOICES,
        required=True,
        help_text=(
            "Step outcome. Must be in the active step's outcome set. "
            "'rejected' and 'expired' are reserved for system transitions."
        ),
    )
    outcome_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Explanation for the outcome.",
    )
    internal_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Internal notes captured by the call-management team. Stored on "
            "the step instance and never returned to applicants."
        ),
    )
    override_budget = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Approve the award although it exceeds what remains of the call's "
            "budget. Needs budget_override_reason. On a call enforcing its "
            "budget strictly, only staff may override."
        ),
    )
    budget_override_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Why the budget is overridden; recorded on the proposal's log.",
    )

    def validate(self, attrs):
        if (
            attrs.get("override_budget")
            and not attrs.get("budget_override_reason", "").strip()
        ):
            raise serializers.ValidationError(
                {
                    "budget_override_reason": _(
                        "Give a reason for overriding the budget."
                    )
                }
            )
        if not attrs.get("override_budget"):
            attrs["budget_override_reason"] = ""
        return attrs

    def validate_outcome(self, value):
        if value in WorkflowStepOutcomes.SYSTEM_RESERVED:
            raise serializers.ValidationError(
                f"'{value}' is system-reserved and cannot be supplied here."
            )
        active_step = self.context.get("active_step")
        if active_step:
            allowed = active_step.allowed_outcomes
            if value not in allowed:
                raise serializers.ValidationError(
                    f"'{value}' is not a valid outcome for step "
                    f"'{active_step.key}'. Allowed: {sorted(allowed)}."
                )
        return value


class RejectWorkflowStepSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=True,
        help_text=(
            "UUID of the workflow step instance the client believes is active. "
            "Used to detect concurrent step transitions."
        ),
    )
    reason = serializers.CharField(
        required=True,
        help_text="Reason for rejecting the proposal at this step.",
    )
    internal_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Internal notes captured by the call-management team alongside "
            "the rejection. Never returned to applicants."
        ),
    )


class ReopenDecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=True,
        help_text=(
            "Why the held decision is taken back, e.g. the board changed the "
            "outcome when adopting the list. Never sent to the applicant and "
            "kept out of the proposal's event feed, which the applicant team "
            "reads; recorded in the server log."
        ),
    )
    deadline = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "New deadline for the decision. Without one, a deadline that has "
            "passed is cleared and one still ahead is kept."
        ),
    )

    def validate_deadline(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError(_("The deadline must be in the future."))
        return value


class CompleteWorkflowStepResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    proposal_state = serializers.CharField(
        required=False,
        help_text="New proposal state when the workflow terminates.",
    )
    next_step = serializers.CharField(
        required=False,
        help_text="Key of the step that just became active.",
    )
    next_step_uuid = serializers.UUIDField(
        required=False,
        format="hex",
        help_text="UUID of the call step that just became active.",
    )


class RejectWorkflowStepResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    proposal_state = serializers.CharField()


class DashboardReviewDeadlineSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    proposal_uuid = serializers.UUIDField(read_only=True)
    proposal_name = serializers.CharField(read_only=True)
    call_uuid = serializers.UUIDField(read_only=True)
    call_name = serializers.CharField(read_only=True)
    due_date = serializers.DateTimeField(read_only=True, allow_null=True)


class DashboardReviewerStatsSerializer(serializers.Serializer):
    assigned = serializers.IntegerField(read_only=True)
    pending = serializers.IntegerField(read_only=True)
    completed = serializers.IntegerField(read_only=True)
    deadlines = DashboardReviewDeadlineSerializer(many=True, read_only=True)
    # How many reviews have a deadline at all, which is not `pending`: a review
    # whose round leaves review_duration_in_days unset is pending but has no
    # deadline. `deadlines` above is capped, so this is the only way a client
    # can tell a full list from a truncated one.
    deadlines_total = serializers.IntegerField(read_only=True)


class DashboardCallManagerStatsSerializer(serializers.Serializer):
    pending_assessments = serializers.IntegerField(read_only=True)
    active_calls = serializers.IntegerField(read_only=True)
    reviews_due_soon = serializers.IntegerField(read_only=True)
    # The window behind reviews_due_soon, so a client can open the matching
    # list (proposal-reviews ?due_within_days=) without hardcoding it.
    reviews_due_within_days = serializers.IntegerField(read_only=True)


class DashboardUpcomingDeadlineSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    call_uuid = serializers.UUIDField(read_only=True)
    call_name = serializers.CharField(read_only=True)
    round_name = serializers.CharField(read_only=True)
    due_date = serializers.DateTimeField(read_only=True, allow_null=True)


class DashboardSubmitterStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField(read_only=True)
    draft = serializers.IntegerField(read_only=True)
    submitted = serializers.IntegerField(read_only=True)
    in_review = serializers.IntegerField(read_only=True)
    accepted = serializers.IntegerField(read_only=True)
    rejected = serializers.IntegerField(read_only=True)
    canceled = serializers.IntegerField(read_only=True)


class PublishRoundResultsSerializer(serializers.Serializer):
    force = serializers.BooleanField(
        default=False,
        help_text="Publish although some proposals of the round have no decision.",
    )
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Why results are published early. Required with force.",
    )

    def validate(self, attrs):
        if attrs["force"] and not attrs["reason"].strip():
            raise serializers.ValidationError(
                {"reason": _("A reason is required to publish with force.")}
            )
        return attrs


class PublishRoundResultsRefusalSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True)
    undecided_count = serializers.IntegerField(
        read_only=True,
        help_text=(
            "Proposals of the round without a decision. Present when publishing "
            "is refused because of them; publish with force and a reason to go "
            "ahead anyway."
        ),
    )


class PublishRoundResultsFailureSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)


class PublishRoundResultsResponseSerializer(ProtectedRoundSerializer):
    failed_proposals = PublishRoundResultsFailureSerializer(
        many=True,
        read_only=True,
        help_text=(
            "Proposals whose held decision could not be carried out. They stay "
            "held; publishing the round's results again retries them."
        ),
    )

    class Meta(ProtectedRoundSerializer.Meta):
        fields = ProtectedRoundSerializer.Meta.fields + ["failed_proposals"]


class CompleteRoundRefusalSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True)
    held_decisions_count = serializers.IntegerField(
        read_only=True,
        required=False,
        help_text=(
            "Decisions of the round whose release failed at publication and "
            "that were never announced. Present when completing is refused "
            "because of them; publish the results again to retry them."
        ),
    )
    undecided_count = serializers.IntegerField(
        read_only=True,
        required=False,
        help_text=(
            "Proposals of the round without a decision. Present when "
            "completing is refused because of them under the refuse rule."
        ),
    )


class CompleteRoundResponseSerializer(ProtectedRoundSerializer):
    rejected_proposals = PublishRoundResultsFailureSerializer(
        many=True,
        read_only=True,
        help_text=("Proposals without a decision that completing the round rejected."),
    )
    failed_proposals = PublishRoundResultsFailureSerializer(
        many=True,
        read_only=True,
        help_text=(
            "Proposals without a decision whose rejection failed. The round "
            "stays open; completing it again retries them."
        ),
    )

    class Meta(ProtectedRoundSerializer.Meta):
        fields = ProtectedRoundSerializer.Meta.fields + [
            "rejected_proposals",
            "failed_proposals",
        ]


class RoundAdoptionSerializer(serializers.ModelSerializer):
    """The adoption of a round's results by the body entitled to adopt them."""

    class Meta:
        model = models.Round
        fields = ["adopted_at", "adoption_note", "adoption_document"]
        extra_kwargs = {
            "adopted_at": {"required": True},
            "adoption_document": {"validators": [DocumentValidator]},
        }


# =============================================================================
# Reviewers suggested for a proposal by its evaluators
# =============================================================================


class ProposalReviewerSuggestionSerializer(serializers.ModelSerializer):
    """A reviewer suggested by one evaluator, as its suggester sees it."""

    proposal = serializers.SlugRelatedField(slug_field="uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    suggested_user = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    suggested_user_name = serializers.ReadOnlyField(
        source="suggested_user.full_name", allow_null=True
    )
    suggester_uuid = serializers.UUIDField(
        source="contribution.contributor.uuid",
        read_only=True,
        allow_null=True,
        format="hex",
    )
    suggester_name = serializers.ReadOnlyField(
        source="contribution.contributor.full_name", allow_null=True
    )
    step_name = serializers.ReadOnlyField(
        source="contribution.proposal_step.call_step.name"
    )
    status = serializers.ChoiceField(
        source="current_status",
        choices=enums.ProposalReviewerSuggestionStatuses.CHOICES,
        read_only=True,
        help_text="Where the suggestion stands; follows its pool invitation.",
    )

    class Meta:
        model = models.ProposalReviewerSuggestion
        fields = [
            "uuid",
            "created",
            "proposal",
            "proposal_name",
            "suggested_user",
            "suggested_user_name",
            "suggested_email",
            "note",
            "suggester_uuid",
            "suggester_name",
            "step_name",
            "status",
        ]
        read_only_fields = fields


class ProposalReviewerSuggestionCreateSerializer(serializers.Serializer):
    suggested_user = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=core_models.User.objects.filter(is_active=True),
        required=False,
        allow_null=True,
        help_text="An existing user suggested as a reviewer.",
    )
    suggested_email = serializers.EmailField(
        required=False,
        allow_blank=True,
        help_text="The e-mail address of a reviewer without a user account.",
    )
    note = serializers.CharField(
        required=False, allow_blank=True, help_text="Why this reviewer: expertise."
    )


class ProposalReviewerSuggestionWithdrawSerializer(serializers.Serializer):
    suggestion_uuid = serializers.UUIDField()


class ReviewerSuggestionEntrySerializer(serializers.Serializer):
    """One evaluator's suggestion of a person."""

    uuid = serializers.UUIDField(format="hex")
    suggester_uuid = serializers.UUIDField(format="hex", allow_null=True)
    suggester_name = serializers.CharField(allow_null=True)
    step_name = serializers.CharField()
    note = serializers.CharField(allow_blank=True)
    created = serializers.DateTimeField()

    def to_representation(self, instance):
        contributor = instance.contribution.contributor
        return {
            "uuid": instance.uuid.hex,
            "suggester_uuid": contributor.uuid.hex if contributor else None,
            "suggester_name": contributor.full_name if contributor else None,
            "step_name": instance.contribution.proposal_step.call_step.name,
            "note": instance.note,
            "created": serializers.DateTimeField().to_representation(instance.created),
        }


class SuggestedReviewerConflictSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    status = serializers.CharField()
    coi_type = serializers.CharField()
    severity = serializers.CharField()


class ReviewerSuggestionCandidateSerializer(serializers.Serializer):
    """A registered user an evaluator may suggest as a reviewer.

    Carries no e-mail address: evaluators pick people, they do not collect
    their addresses."""

    uuid = serializers.UUIDField(format="hex")
    full_name = serializers.CharField()
    affiliation = serializers.CharField(
        allow_null=True,
        help_text="The current affiliation on the person's reviewer profile.",
    )
    has_published_profile = serializers.BooleanField(
        help_text="The person published their reviewer profile."
    )
    pool_status = serializers.ChoiceField(
        choices=[
            choice
            for choice in enums.ProposalReviewerSuggestionStatuses.CHOICES
            if choice[0]
            in (
                enums.ProposalReviewerSuggestionStatuses.INVITED,
                enums.ProposalReviewerSuggestionStatuses.ACCEPTED,
                enums.ProposalReviewerSuggestionStatuses.DECLINED,
                enums.ProposalReviewerSuggestionStatuses.EXPIRED,
            )
        ],
        allow_null=True,
        help_text=(
            "Where the person stands in the call's reviewer pool; null when "
            "they are not in it."
        ),
    )
    has_conflict = serializers.BooleanField(
        help_text="A conflict of interest stands between the person and the proposal."
    )


class SuggestedReviewerSerializer(serializers.Serializer):
    """A person suggested for a proposal, with every suggestion of them."""

    key = serializers.CharField(
        help_text="Identifies the suggested person within the proposal."
    )
    suggested_user_uuid = serializers.UUIDField(
        source="user.uuid", format="hex", allow_null=True, default=None
    )
    suggested_user_name = serializers.CharField(
        source="user.full_name", allow_null=True, default=None
    )
    email = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The person's address, for staff, support and those overseeing "
            "the call; for anyone else only an address an evaluator typed, "
            "blank for a user they picked."
        ),
    )
    status = serializers.ChoiceField(
        choices=enums.ProposalReviewerSuggestionStatuses.CHOICES
    )
    pool_entry_uuid = serializers.UUIDField(
        source="pool_entry.uuid", format="hex", allow_null=True, default=None
    )
    invitation_expires_at = serializers.DateTimeField(
        source="pool_entry.invitation_expires_at", allow_null=True, default=None
    )
    suggestions = ReviewerSuggestionEntrySerializer(many=True)
    suggestion_uuids = serializers.SerializerMethodField()
    conflicts = SuggestedReviewerConflictSerializer(many=True)
    has_conflict = serializers.BooleanField(
        help_text="A conflict of interest stands between the person and the proposal."
    )
    is_applicant_excluded = serializers.BooleanField(
        help_text="The applicant asked for the person not to review the proposal."
    )
    holds_evaluator_role = serializers.BooleanField()
    is_on_applicant_team = serializers.BooleanField()
    already_in_pool = serializers.BooleanField()
    assignment_batch_status = serializers.ChoiceField(
        choices=enums.AssignmentBatchStatuses.CHOICES,
        allow_null=True,
        default=None,
        help_text=(
            "Where the latest assignment of the person to the proposal "
            "stands; draft until the call manager sends its batch."
        ),
    )

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_suggestion_uuids(self, candidate):
        return [s.uuid.hex for s in candidate.suggestions]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.user is not None and not self.context.get("reads_addresses", True):
            data["email"] = ""
        return data


class SuggestedReviewersActionSerializer(serializers.Serializer):
    suggestion_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text=(
            "Suggestions naming the people to act on; every suggestion of "
            "each person for the proposal is acted on."
        ),
    )


class SuggestedReviewerInvitationSerializer(serializers.Serializer):
    candidate = SuggestedReviewerSerializer()
    outcome = serializers.ChoiceField(
        choices=enums.ReviewerInvitationOutcomes.CHOICES,
        help_text=(
            "invited: a new pool invitation was sent; resent: their expired "
            "invitation was sent again; linked: linked to the pool entry they "
            "already had; refused: not invited, the detail says why."
        ),
    )
    invited = serializers.BooleanField(
        help_text="Whether an invitation e-mail is sent: invited or resent."
    )
    detail = serializers.CharField(allow_blank=True)


class InviteSuggestedReviewersResponseSerializer(serializers.Serializer):
    invited = serializers.IntegerField(help_text="New pool invitations sent.")
    resent = serializers.IntegerField(help_text="Expired invitations sent again.")
    linked = serializers.IntegerField(
        help_text="People linked to the pool entry they already had."
    )
    refused = serializers.IntegerField(help_text="People not invited.")
    results = SuggestedReviewerInvitationSerializer(many=True)


class RecommendedComponentSerializer(serializers.Serializer):
    type = serializers.CharField()
    name = serializers.CharField()
    measured_unit = serializers.CharField(allow_blank=True)
    amount = serializers.FloatField()


class RecommendedOfferingSerializer(serializers.Serializer):
    requested_offering_uuid = serializers.CharField()
    offering_name = serializers.CharField()
    components = RecommendedComponentSerializer(many=True)


class ProposalRankingSerializer(serializers.ModelSerializer):
    round = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Round.objects.all()
    )
    # A proposal that does not exist, that the user does not read or that
    # is of another round is refused alike: ranking tells nothing of
    # proposals beyond those the user ranks.
    UNKNOWN_PROPOSAL = _("No such proposal in this round.")

    proposal = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.Proposal.objects.all(),
        error_messages={"does_not_exist": UNKNOWN_PROPOSAL},
    )
    meeting_note = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.RoundMeetingNote.objects.filter(call_step__isnull=False),
        required=False,
        allow_null=True,
        help_text=(
            "The round report the ranking belongs to; empty for the round's "
            "overall ranking."
        ),
    )
    meeting_note_title = serializers.ReadOnlyField(
        source="meeting_note.title", allow_null=True
    )
    round_uuid = serializers.UUIDField(source="round.uuid", read_only=True)
    round_name = serializers.ReadOnlyField(source="round.name")
    call_uuid = serializers.UUIDField(source="round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="round.call.name")
    proposal_uuid = serializers.UUIDField(source="proposal.uuid", read_only=True)
    proposal_name = serializers.ReadOnlyField(source="proposal.name")
    proposal_slug = serializers.ReadOnlyField(source="proposal.slug")
    proposal_state = serializers.ReadOnlyField(source="proposal.state")
    proposal_panel_uuid = serializers.UUIDField(
        source="proposal.panel.uuid", format="hex", read_only=True, allow_null=True
    )
    proposal_panel_name = serializers.ReadOnlyField(
        source="proposal.panel.name", allow_null=True
    )
    # Validated as a line of an imported ranking is.
    rank = serializers.IntegerField(min_value=1, max_value=rankings.MAX_RANK)
    final_score = serializers.DecimalField(
        max_digits=10,
        decimal_places=3,
        required=False,
        allow_null=True,
        coerce_to_string=False,
    )
    created_by_name = serializers.SerializerMethodField()
    is_locked = serializers.SerializerMethodField()
    recommended_resources_detail = serializers.SerializerMethodField(
        help_text=(
            "The recommendation named for reading: per requested offering, "
            "its name and each recommended component's name, unit and amount."
        )
    )

    class Meta:
        model = models.ProposalRanking
        fields = [
            "uuid",
            "round",
            "round_uuid",
            "round_name",
            "call_uuid",
            "call_name",
            "proposal",
            "proposal_uuid",
            "proposal_name",
            "proposal_slug",
            "proposal_state",
            "proposal_panel_uuid",
            "proposal_panel_name",
            "meeting_note",
            "meeting_note_title",
            "rank",
            "final_score",
            "recommended_resources",
            "recommended_resources_detail",
            "notes",
            "created_by_name",
            "is_locked",
            "created",
            "modified",
        ]
        read_only_fields = ["uuid", "created", "modified"]
        # validate() refuses a taken rank or a ranked proposal with a message
        # naming it; the generic unique-together validators would answer first.
        validators = []

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_created_by_name(self, obj):
        return obj.created_by.full_name if obj.created_by else None

    @extend_schema_field(serializers.BooleanField())
    def get_is_locked(self, obj) -> bool:
        return rankings.is_ranking_locked(obj.round) or bool(
            obj.meeting_note_id and obj.meeting_note.submitted_at
        )

    @extend_schema_field(RecommendedOfferingSerializer(many=True))
    def get_recommended_resources_detail(self, obj):
        return RecommendedOfferingSerializer(
            rankings.recommended_resources_detail(obj), many=True
        ).data

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None and not isinstance(self.instance, list):
            # The round, the proposal and the report of a ranking are fixed.
            fields["round"].read_only = True
            fields["proposal"].read_only = True
            fields["meeting_note"].read_only = True
        return fields

    def validate(self, attrs):
        instance = self.instance
        call_round = attrs.get("round") or instance.round
        proposal = attrs.get("proposal") or instance.proposal
        note = attrs.get("meeting_note") if instance is None else instance.meeting_note
        request = self.context["request"]
        if note is not None and note.round_id != call_round.pk:
            raise serializers.ValidationError(
                {"meeting_note": _("The round report is of another round.")}
            )
        rankings.ensure_can_write_ranking(request.user, call_round, note)
        if instance is None and (
            proposal.round_id != call_round.pk
            or not filter_queryset_for_user(
                models.Proposal.objects.filter(pk=proposal.pk), request.user
            ).exists()
        ):
            raise serializers.ValidationError({"proposal": self.UNKNOWN_PROPOSAL})
        rankings.ensure_not_on_team(request.user, proposal)
        rankings.ensure_ranking_open(call_round)
        rankings.validate_proposal(call_round, proposal, note)
        if "recommended_resources" in attrs:
            attrs["recommended_resources"] = rankings.validate_recommended_resources(
                proposal, attrs["recommended_resources"]
            )
        if (
            instance is None
            and models.ProposalRanking.objects.filter(
                round=call_round, meeting_note=note, proposal=proposal
            ).exists()
        ):
            message = (
                _("The proposal is already ranked in this round report.")
                if note is not None
                else _("The proposal is already ranked in this round.")
            )
            raise serializers.ValidationError({"proposal": message})
        if "rank" in attrs:
            rankings.ensure_rank_free(
                call_round,
                attrs["rank"],
                exclude_pk=instance.pk if instance else None,
                meeting_note=note,
            )
        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class RankingImportSerializer(serializers.Serializer):
    round = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Round.objects.all()
    )
    meeting_note = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.RoundMeetingNote.objects.filter(call_step__isnull=False),
        required=False,
        allow_null=True,
        help_text=(
            "The round report to import the ranking of; empty for the round's "
            "overall ranking."
        ),
    )
    file = serializers.FileField(
        help_text=(
            "CSV with a header row: proposal_uuid or proposal_slug, rank, "
            "final_score, notes, and recommended:<requested offering uuid>:"
            "<component type> per recommended amount -- the columns the export "
            "writes."
        )
    )


class RankingImportResponseSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    updated = serializers.IntegerField()


class RoundMeetingNoteDocumentSerializer(serializers.ModelSerializer):
    name = serializers.ReadOnlyField()

    class Meta:
        model = models.RoundMeetingNoteDocument
        fields = ["uuid", "file", "name", "created"]
        read_only_fields = fields


class RoundReportProposalSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    name = serializers.CharField()
    slug = serializers.CharField()


class RoundMeetingNoteSerializer(serializers.ModelSerializer):
    round = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Round.objects.all()
    )
    call_step_uuid = serializers.UUIDField(
        source="call_step.uuid",
        read_only=True,
        allow_null=True,
        format="hex",
        help_text="The round step a round report is for; null for a free note.",
    )
    call_step_name = serializers.ReadOnlyField(source="call_step.name", allow_null=True)
    is_report = serializers.ReadOnlyField()
    report_proposals = serializers.SerializerMethodField(
        help_text="The proposals a round report covers."
    )
    has_checklist = serializers.SerializerMethodField(
        help_text="Whether the round report answers its step's form."
    )
    can_edit = serializers.SerializerMethodField()
    round_uuid = serializers.UUIDField(source="round.uuid", read_only=True)
    round_name = serializers.ReadOnlyField(source="round.name")
    call_uuid = serializers.UUIDField(source="round.call.uuid", read_only=True)
    call_name = serializers.ReadOnlyField(source="round.call.name")
    author_name = serializers.SerializerMethodField()
    author_uuid = serializers.SerializerMethodField()
    documents = RoundMeetingNoteDocumentSerializer(many=True, read_only=True)
    new_documents = serializers.ListField(
        child=serializers.FileField(validators=[DocumentValidator]),
        write_only=True,
        required=False,
        help_text="Files to attach to the note.",
    )
    removed_documents = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="UUIDs of attached documents to remove.",
    )

    class Meta:
        model = models.RoundMeetingNote
        fields = [
            "uuid",
            "round",
            "round_uuid",
            "round_name",
            "call_uuid",
            "call_name",
            "title",
            "body",
            "author_name",
            "author_uuid",
            "call_step_uuid",
            "call_step_name",
            "is_report",
            "report_proposals",
            "submitted_at",
            "has_checklist",
            "can_edit",
            "documents",
            "new_documents",
            "removed_documents",
            "created",
            "modified",
        ]
        read_only_fields = ["uuid", "created", "modified", "submitted_at"]

    @extend_schema_field(RoundReportProposalSerializer(many=True))
    def get_report_proposals(self, obj):
        if not obj.is_report:
            return []
        return [
            {"uuid": proposal.uuid.hex, "name": proposal.name, "slug": proposal.slug}
            for proposal in obj.proposals.order_by("name", "id")
        ]

    @extend_schema_field(serializers.BooleanField())
    def get_has_checklist(self, obj) -> bool:
        return bool(obj.is_report and obj.call_step.checklist_id)

    @extend_schema_field(serializers.BooleanField())
    def get_can_edit(self, obj) -> bool:
        request = self.context.get("request")
        if not request:
            return False
        return rankings.can_write_note(request.user, obj)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_author_name(self, obj):
        return obj.author.full_name if obj.author else None

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_author_uuid(self, obj):
        return obj.author.uuid.hex if obj.author else None

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None and not isinstance(self.instance, list):
            fields["round"].read_only = True
        return fields

    def validate(self, attrs):
        user = self.context["request"].user
        if self.instance is not None and self.instance.is_report:
            if not rankings.can_write_note(user, self.instance):
                raise PermissionDenied(
                    _(
                        "Only the author of a round report edits it, until it "
                        "is submitted."
                    )
                )
            return attrs
        call_round = attrs.get("round") or self.instance.round
        rankings.ensure_can_write(user, call_round)
        return attrs

    def _sync_documents(self, note, new_documents, removed_documents):
        if removed_documents:
            note.documents.filter(uuid__in=removed_documents).delete()
        for file in new_documents or []:
            models.RoundMeetingNoteDocument.objects.create(note=note, file=file)

    @transaction.atomic
    def create(self, validated_data):
        new_documents = validated_data.pop("new_documents", [])
        validated_data.pop("removed_documents", None)
        validated_data["author"] = self.context["request"].user
        note = super().create(validated_data)
        self._sync_documents(note, new_documents, None)
        return note

    @transaction.atomic
    def update(self, instance, validated_data):
        new_documents = validated_data.pop("new_documents", [])
        removed_documents = validated_data.pop("removed_documents", [])
        note = super().update(instance, validated_data)
        self._sync_documents(note, new_documents, removed_documents)
        return note


class ProposalFeedbackSerializer(serializers.Serializer):
    proposal_uuid = serializers.UUIDField()
    feedback = serializers.CharField(allow_blank=True)


class RoundStepProposalResultSerializer(serializers.Serializer):
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    detail = serializers.CharField()
    proposal_state = serializers.CharField(required=False)
    current_step_name = serializers.CharField(required=False, allow_null=True)


class SubmitRoundReportResponseSerializer(serializers.Serializer):
    report = RoundMeetingNoteSerializer()
    completed = RoundStepProposalResultSerializer(many=True)
    failed = RoundStepProposalResultSerializer(many=True)


class _RoundStepChoiceMixin:
    """Names a call step of the round's call by step_uuid or its key."""

    def _call_step(self, attrs):
        call_round = self.context["round"]
        steps = models.CallWorkflowStep.objects.filter(call=call_round.call)
        if attrs.get("step_uuid"):
            call_step = steps.filter(uuid=attrs["step_uuid"]).first()
        elif attrs.get("step"):
            call_step = steps.filter(key=attrs["step"]).first()
        else:
            raise serializers.ValidationError(
                {"step": _("Name the step by step or step_uuid.")}
            )
        if call_step is None:
            raise serializers.ValidationError({"step": _("The call has no such step.")})
        return call_step


class OpenRoundWorkflowStepSerializer(_RoundStepChoiceMixin, serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=False, help_text="UUID of the round step. Either this or step."
    )
    step = serializers.CharField(
        required=False, help_text="Key of the round step. Either this or step_uuid."
    )
    due_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="When the step is due in this round; left as it is when omitted.",
    )
    force = serializers.BooleanField(
        default=False,
        help_text=(
            "Open the step although some proposals of the round have not "
            "reached it yet; they join it as they arrive."
        ),
    )
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Why the step is opened early. Required with force.",
    )

    def validate(self, attrs):
        call_step = self._call_step(attrs)
        if not call_step.round_scoped:
            raise serializers.ValidationError(
                {"step": _("The step runs on each proposal, not once per round.")}
            )
        if not call_step.is_enabled:
            raise serializers.ValidationError({"step": _("The step is disabled.")})
        if attrs["force"] and not attrs["reason"].strip():
            raise serializers.ValidationError(
                {
                    "reason": _(
                        "Say why the step is opened before every proposal reached it."
                    )
                }
            )
        attrs["call_step"] = call_step
        return attrs


class OpenRoundWorkflowStepResponseSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField()
    step_name = serializers.CharField()
    opened_at = serializers.DateTimeField()
    due_at = serializers.DateTimeField(allow_null=True)
    reports_created = serializers.IntegerField()
    advanced = RoundStepProposalResultSerializer(many=True)
    skipped = RoundStepProposalResultSerializer(many=True)
    failed = RoundStepProposalResultSerializer(many=True)


class LaggingProposalsRefusalSerializer(serializers.Serializer):
    detail = serializers.CharField()
    lagging_count = serializers.IntegerField()


class AdvanceRoundWorkflowStepSerializer(_RoundStepChoiceMixin, serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=False,
        help_text="UUID of the completed step to move on from. Either this or step.",
    )
    step = serializers.CharField(
        required=False,
        help_text="Key of the completed step to move on from. Either this or step_uuid.",
    )

    def validate(self, attrs):
        call_step = self._call_step(attrs)
        if call_step.transition_mode != enums.TransitionModes.MANUAL:
            raise serializers.ValidationError(
                {"step": _("The step moves on by itself; nothing waits on it.")}
            )
        attrs["call_step"] = call_step
        return attrs


class AdvanceRoundWorkflowStepResponseSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField()
    step_name = serializers.CharField()
    advanced = RoundStepProposalResultSerializer(many=True)
    skipped = RoundStepProposalResultSerializer(many=True)
    failed = RoundStepProposalResultSerializer(many=True)


class RoundWorkflowStepDueDateSerializer(_RoundStepChoiceMixin, serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=False, help_text="UUID of the step. Either this or step."
    )
    step = serializers.CharField(
        required=False, help_text="Key of the step. Either this or step_uuid."
    )
    due_at = serializers.DateTimeField(
        allow_null=True,
        help_text=(
            "When the step is due for every proposal of the round; null to "
            "count the step's duration again."
        ),
    )

    def validate(self, attrs):
        attrs["call_step"] = self._call_step(attrs)
        if attrs["due_at"] is not None and attrs["due_at"] <= timezone.now():
            raise serializers.ValidationError(
                {"due_at": _("The due date must be in the future.")}
            )
        return attrs


class CompleteRoundWorkflowStepSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField(
        required=False,
        help_text="UUID of the call step to complete. Either this or step.",
    )
    step = serializers.CharField(
        required=False,
        help_text="Key of the call step to complete. Either this or step_uuid.",
    )
    outcome = serializers.ChoiceField(
        choices=WorkflowStepOutcomes.CHOICES,
        help_text="The outcome every proposal at the step is completed with.",
    )
    feedback = ProposalFeedbackSerializer(
        many=True,
        required=False,
        help_text=(
            "Feedback per proposal, stored as that proposal's outcome reason "
            "for the step. The applicant reads it only once the step's report "
            "is released."
        ),
    )

    def validate(self, attrs):
        call_round = self.context["round"]
        steps = models.CallWorkflowStep.objects.filter(call=call_round.call)
        if attrs.get("step_uuid"):
            call_step = steps.filter(uuid=attrs["step_uuid"]).first()
        elif attrs.get("step"):
            # A step's key is unique within its call.
            call_step = steps.filter(key=attrs["step"]).first()
        else:
            raise serializers.ValidationError(
                {"step": _("Name the step by step or step_uuid.")}
            )
        if call_step is None:
            raise serializers.ValidationError({"step": _("The call has no such step.")})
        if call_step.is_allocation_decision:
            raise serializers.ValidationError(
                {
                    "step": _(
                        "The allocation decision settles each proposal's award, "
                        "so it is completed on each proposal, not for the "
                        "whole round."
                    )
                }
            )
        if call_step.is_post_award:
            raise serializers.ValidationError(
                {
                    "step": _(
                        "A step of an extension or closing runs on each "
                        "accepted proposal's own run, not for the whole round."
                    )
                }
            )
        outcome = attrs["outcome"]
        if outcome in WorkflowStepOutcomes.SYSTEM_RESERVED:
            raise serializers.ValidationError(
                {"outcome": _("This outcome is reserved for the system.")}
            )
        if outcome not in call_step.allowed_outcomes:
            raise serializers.ValidationError(
                {
                    "outcome": _("'%(outcome)s' is not an outcome of step %(step)s.")
                    % {"outcome": outcome, "step": call_step.name or call_step.key}
                }
            )
        feedback = {}
        round_proposals = set(
            models.Proposal.objects.filter(round=call_round).values_list(
                "uuid", flat=True
            )
        )
        round_proposals = {uuid.hex for uuid in round_proposals}
        for item in attrs.get("feedback") or []:
            proposal_uuid = item["proposal_uuid"].hex
            if proposal_uuid not in round_proposals:
                raise serializers.ValidationError(
                    {
                        "feedback": _("%(uuid)s is not a proposal of this round.")
                        % {"uuid": proposal_uuid}
                    }
                )
            if item["feedback"]:
                feedback[proposal_uuid] = item["feedback"]
        attrs["call_step"] = call_step
        attrs["feedback_by_proposal"] = feedback
        return attrs


class CompleteRoundWorkflowStepResponseSerializer(serializers.Serializer):
    step_uuid = serializers.UUIDField()
    step_name = serializers.CharField()
    outcome = serializers.CharField()
    completed = RoundStepProposalResultSerializer(many=True)
    skipped = RoundStepProposalResultSerializer(many=True)
    failed = RoundStepProposalResultSerializer(many=True)


class AllocateInSubmissionOrderSerializer(serializers.Serializer):
    preview = serializers.BooleanField(
        default=True,
        help_text="Only plan the decisions and return the plan; decide nothing.",
    )
    on_depleted = serializers.ChoiceField(
        choices=enums.OnDepletedBudget.CHOICES,
        default=enums.OnDepletedBudget.DECLINE,
        help_text=(
            "What to do with a proposal whose award no longer fits what "
            "remains: decline it, or leave it for a decision by hand. Either "
            "way later, smaller proposals still get their chance."
        ),
    )
    force = serializers.BooleanField(
        default=False,
        help_text=(
            "Allocate although some proposals of the round have not reached "
            "the allocation decision yet. Needs a reason."
        ),
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["force"] and not attrs["reason"].strip():
            raise serializers.ValidationError(
                {
                    "reason": _(
                        "Give a reason for allocating before every proposal is ready."
                    )
                }
            )
        return attrs


class AllocationPlanBudgetSerializer(serializers.Serializer):
    pool_uuid = serializers.UUIDField(source="pool.uuid")
    offering_name = serializers.CharField()
    component_type = serializers.CharField()
    round_name = serializers.CharField(
        source="round.name",
        allow_null=True,
        help_text="The round the pool covers; empty for the whole call.",
    )
    offered_total = LimitValueField()
    remaining_before = LimitValueField()
    remaining_after = LimitValueField()


class AllocationPlanAwardSerializer(serializers.Serializer):
    offering_name = serializers.CharField()
    component_type = serializers.CharField()
    amount = LimitValueField(
        help_text=(
            "What the award draws from the pools: a periodic amount counted "
            "once per period the item runs."
        )
    )


class AllocationPlanRowSerializer(serializers.Serializer):
    order = serializers.IntegerField()
    proposal_uuid = serializers.UUIDField()
    proposal_name = serializers.CharField()
    proposal_slug = serializers.CharField(allow_blank=True)
    submitted_at = serializers.DateTimeField(allow_null=True)
    score = serializers.FloatField(allow_null=True)
    threshold = serializers.FloatField(allow_null=True)
    award = AllocationPlanAwardSerializer(many=True)
    planned_outcome = serializers.ChoiceField(
        choices=(
            ("approved", "Approved"),
            ("declined", "Declined"),
            ("left", "Left undecided"),
            ("skipped", "Skipped"),
        )
    )
    reason = serializers.CharField(allow_blank=True)


class AllocateInSubmissionOrderResponseSerializer(serializers.Serializer):
    preview = serializers.BooleanField()
    has_budget = serializers.BooleanField(
        help_text="False when no resource pool of the call covers this round."
    )
    plan = AllocationPlanRowSerializer(many=True)
    budgets = AllocationPlanBudgetSerializer(many=True)
    completed = RoundStepProposalResultSerializer(many=True)
    skipped = RoundStepProposalResultSerializer(many=True)
    failed = RoundStepProposalResultSerializer(many=True)


class MyTaskSummarySerializer(serializers.Serializer):
    """How much open work waits for the current user, and how urgent it is."""

    total = serializers.IntegerField(help_text="Every open task.")
    overdue = serializers.IntegerField(help_text="Tasks past their deadline.")
    due_soon = serializers.IntegerField(
        help_text="Tasks due within due_soon_days, not yet past their deadline."
    )
    due_soon_days = serializers.IntegerField(
        help_text="How many days ahead a deadline counts as due soon."
    )


class MyTaskSerializer(serializers.Serializer):
    """One open piece of workflow work waiting for the current user."""

    key = serializers.CharField(
        help_text="Identifies the task among the user's tasks; stable while it is open."
    )
    needed = serializers.ChoiceField(
        choices=my_tasks.Needs.CHOICES, help_text="What the task asks of the user."
    )
    needed_label = serializers.CharField()
    detail = serializers.CharField(
        allow_blank=True,
        help_text=(
            "What the task is about within the step: the offering a "
            "per-offering slot assesses, or the evaluator role to confirm."
        ),
    )
    proposal_uuid = serializers.UUIDField(
        source="proposal.uuid", allow_null=True, default=None, format="hex"
    )
    proposal_name = serializers.CharField(
        source="proposal.name", allow_null=True, default=None
    )
    proposal_slug = serializers.CharField(
        source="proposal.slug", allow_null=True, default=None
    )
    call_uuid = serializers.UUIDField(
        source="call.uuid", allow_null=True, default=None, format="hex"
    )
    call_name = serializers.CharField(source="call.name", allow_null=True, default=None)
    round_uuid = serializers.UUIDField(
        source="call_round.uuid", allow_null=True, default=None, format="hex"
    )
    offering_uuid = serializers.UUIDField(
        source="offering.uuid",
        allow_null=True,
        default=None,
        format="hex",
        help_text=(
            "The offering the task is about: the one a per-offering slot "
            "assesses, or the one an offering request asks to include."
        ),
    )
    offering_name = serializers.CharField(
        source="offering.name", allow_null=True, default=None
    )
    provider_uuid = serializers.UUIDField(
        source="offering.customer.uuid",
        allow_null=True,
        default=None,
        format="hex",
        help_text="The organization providing the task's offering.",
    )
    provider_name = serializers.CharField(
        source="offering.customer.name", allow_null=True, default=None
    )
    round_name = serializers.CharField(
        source="call_round.name", allow_null=True, default=None
    )
    step_uuid = serializers.UUIDField(
        source="instance.uuid",
        allow_null=True,
        default=None,
        format="hex",
        help_text="The proposal's step instance.",
    )
    call_step_uuid = serializers.UUIDField(
        source="instance.call_step.uuid", allow_null=True, default=None, format="hex"
    )
    step_name = serializers.CharField(
        source="instance.call_step.name", allow_null=True, default=None
    )
    step_kind = serializers.ChoiceField(
        source="instance.call_step.kind",
        choices=StepKinds.CHOICES,
        allow_null=True,
        default=None,
    )
    phase = serializers.ChoiceField(
        source="instance.call_step.phase",
        choices=WorkflowPhases.CHOICES,
        allow_null=True,
        default=None,
        help_text=(
            "The phase of the task's step: the evaluation before the award, or "
            "an extension or closing after it; null for a task on no step."
        ),
    )
    due_at = serializers.DateTimeField(allow_null=True)
    is_overdue = serializers.BooleanField()
    target = serializers.ChoiceField(
        choices=my_tasks.Targets.CHOICES,
        help_text="The page the UI opens the task on.",
    )
    target_section = serializers.CharField(
        source="section",
        allow_null=True,
        help_text="Id of the section of the proposal page to scroll to.",
    )
    target_uuid = serializers.CharField(
        allow_null=True,
        help_text=(
            "The object the target page opens: the review, the assignment "
            "batch, the pool invitation, the evaluator role, the round report, "
            "the round, or the provider organization whose requests for "
            "offerings list the request."
        ),
    )
    coi_declaration_text = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The conflict-of-interest declaration the task has the user "
            "confirm -- taking a contribution over, or holding an evaluator "
            "role. Empty for the platform's standard one, or for a task "
            "asking for none."
        ),
    )
    view = serializers.ChoiceField(
        choices=my_tasks.Views.CHOICES,
        allow_null=True,
        help_text=(
            "Whose view of the proposal page opens a task on a proposal, by "
            "the role that gave the user the task; null for other targets."
        ),
    )
    view_customer_uuid = serializers.UUIDField(
        source="view_customer",
        allow_null=True,
        default=None,
        format="hex",
        help_text=(
            "The organization the view is under: the one managing the call "
            "for the call team's view, the provider for the offering team's."
        ),
    )


# -- workflow description ------------------------------------------------------
# Output-only serializers giving ``workflow_description.describe`` its schema.


class WorkflowDescriptionRefSerializer(serializers.Serializer):
    source = serializers.ChoiceField(
        choices=["step", "call", "rule", "engine"],
        help_text=(
            "Where the reason lives: a field of a step, of the call or of a "
            "notification rule, or something the engine always does."
        ),
    )
    step = serializers.CharField(allow_null=True, help_text="Key of the step.")
    field = serializers.CharField()
    detail = serializers.CharField(allow_null=True)


class WorkflowDescriptionFlagCellSerializer(serializers.Serializer):
    value = serializers.BooleanField()
    because = WorkflowDescriptionRefSerializer(many=True)


class WorkflowDescriptionTriggerSerializer(serializers.Serializer):
    trigger = serializers.CharField()
    days_before = serializers.IntegerField(allow_null=True)


class WorkflowDescriptionNotifiedCellSerializer(serializers.Serializer):
    value = serializers.BooleanField()
    triggers = WorkflowDescriptionTriggerSerializer(many=True)
    because = WorkflowDescriptionRefSerializer(many=True)


class WorkflowDescriptionRoleCellSerializer(serializers.Serializer):
    actor = serializers.CharField(help_text="Key of the actor.")
    sees = WorkflowDescriptionFlagCellSerializer()
    reads_outputs = WorkflowDescriptionFlagCellSerializer()
    contributes = WorkflowDescriptionFlagCellSerializer()
    completes = WorkflowDescriptionFlagCellSerializer()
    notified = WorkflowDescriptionNotifiedCellSerializer()


class WorkflowDescriptionActorSerializer(serializers.Serializer):
    key = serializers.CharField()
    kind = serializers.ChoiceField(
        choices=[
            "applicant",
            "call_manager",
            "panel_chair",
            "panel_member",
            "reviewer",
            "offering_team",
            "proposal_role",
            "proposal_role_lead",
            "system",
        ]
    )
    role_key = serializers.CharField(allow_null=True)
    label = serializers.CharField()
    notes = serializers.ListField(child=serializers.CharField())
    holders = serializers.IntegerField(
        allow_null=True, help_text="How many hold the role on the call; counts only."
    )


class WorkflowDescriptionOutcomeSerializer(serializers.Serializer):
    value = serializers.CharField()
    polarity = serializers.ChoiceField(choices=["positive", "negative"])


class WorkflowDescriptionRunConditionSerializer(serializers.Serializer):
    step = serializers.CharField()
    outcomes = serializers.ListField(
        child=serializers.CharField(),
        help_text="The outcomes of the step it runs on; empty on a score spread.",
    )
    score_spread_above = serializers.FloatField(
        allow_null=True,
        help_text=(
            "Runs only when the scores the step's contributions gave lie more "
            "than this many points apart; null on an outcome condition."
        ),
    )


class WorkflowDescriptionCoiDeclarationSerializer(serializers.Serializer):
    text = serializers.CharField(
        allow_blank=True,
        help_text="The declaration; empty for the platform's standard one.",
    )
    source = serializers.ChoiceField(
        choices=["step", "call", "platform"],
        help_text="Where its wording comes from.",
    )
    editable = serializers.BooleanField(
        help_text="The call team can edit the wording at any time."
    )


class WorkflowDescriptionContributorsSerializer(serializers.Serializer):
    source = serializers.CharField()
    parameter = serializers.CharField(allow_null=True)
    per_requested_offering = serializers.BooleanField()
    lead_only = serializers.BooleanField()
    min = serializers.IntegerField(allow_null=True)
    max = serializers.IntegerField(allow_null=True)


class WorkflowDescriptionDelegationSerializer(serializers.Serializer):
    target = serializers.CharField()
    proposal_role = serializers.CharField(allow_null=True)


class WorkflowDescriptionChecklistSerializer(serializers.Serializer):
    name = serializers.CharField(allow_null=True)
    required = serializers.BooleanField()


class WorkflowDescriptionApplicantSerializer(serializers.Serializer):
    visible = serializers.BooleanField()
    label_kind = serializers.ChoiceField(
        choices=[
            "configured",
            "step_name",
            "awaiting_response",
            "decision",
            "internal_check",
        ]
    )
    label = serializers.CharField(
        help_text="What the applicant's tracker shows while the step is current."
    )
    labels = StepAudienceStatusLabelsSerializer(
        help_text=(
            "The labels the call configured that the applicant reads, by "
            "state; empty when they keep the computed labels."
        )
    )
    internal = serializers.BooleanField()


class WorkflowDescriptionRuleSerializer(serializers.Serializer):
    trigger = serializers.CharField()
    recipient = serializers.CharField()
    proposal_role = serializers.CharField(allow_null=True)
    notified_proposal_roles = serializers.ListField(child=serializers.CharField())
    days_before = serializers.IntegerField(allow_null=True)
    enabled = serializers.BooleanField()
    actors = serializers.ListField(child=serializers.CharField())
    mail_type = serializers.CharField(help_text="The mail type the rule sends.")
    variant = serializers.CharField(
        help_text="The audience the mail is worded for: applicant or call_team."
    )
    customised = serializers.BooleanField(
        help_text="Whether the call words this mail itself."
    )


class WorkflowDescriptionStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    kind = serializers.CharField()
    order = serializers.IntegerField(allow_null=True)
    stage = serializers.IntegerField()
    parallel_with_previous = serializers.BooleanField()
    enabled = serializers.BooleanField()
    phase = serializers.ChoiceField(
        choices=WorkflowPhases.CHOICES,
        help_text=(
            "When the step runs: in the evaluation, or after the award in an "
            "extension or the closing."
        ),
    )
    project_end_offset_days = serializers.IntegerField(
        allow_null=True,
        help_text=(
            "On the first step after the award: days before the project's end "
            "date that time it."
        ),
    )
    outcome_set = serializers.CharField(allow_null=True)
    outcomes = WorkflowDescriptionOutcomeSerializer(many=True)
    on_negative = serializers.CharField(allow_null=True)
    transition_mode = serializers.CharField(allow_null=True)
    duration_in_days = serializers.IntegerField(allow_null=True)
    run_condition = WorkflowDescriptionRunConditionSerializer(allow_null=True)
    input_steps = serializers.ListField(child=serializers.CharField())
    is_allocation_decision = serializers.BooleanField()
    decides_automatically = serializers.BooleanField()
    automatic_decision_guard = serializers.ChoiceField(
        choices=enums.AutomaticDecisionGuards.CHOICES,
        allow_null=True,
        help_text=(
            "What stops an unsuitable proposal before the automatic decision "
            "awards it; null unless the step decides automatically."
        ),
    )
    allocation_time = serializers.CharField(allow_null=True)
    include_award_response = serializers.BooleanField()
    allows_change_requests = serializers.BooleanField()
    allows_conditional_acceptance = serializers.BooleanField()
    completer = serializers.CharField(allow_null=True)
    completer_proposal_role = serializers.CharField(allow_null=True)
    completion_lead_only = serializers.BooleanField()
    assigns_roles = serializers.ListField(child=serializers.CharField())
    contributors = WorkflowDescriptionContributorsSerializer()
    delegable_to = WorkflowDescriptionDelegationSerializer(allow_null=True)
    blind_review = serializers.BooleanField()
    requires_coi_confirmation = serializers.BooleanField()
    coi_declaration = WorkflowDescriptionCoiDeclarationSerializer(
        allow_null=True,
        help_text=(
            "The conflict-of-interest declaration the step's evaluators "
            "confirm; null on a step asking for none."
        ),
    )
    release_to_applicant = serializers.BooleanField()
    collects_reviewer_suggestions = serializers.BooleanField()
    min_score_threshold = serializers.CharField(allow_null=True)
    checklist = WorkflowDescriptionChecklistSerializer(allow_null=True)
    criteria = serializers.IntegerField(help_text="How many criteria the step scores.")
    status_labels = StepStatusLabelsSerializer(
        help_text="The step's status labels as configured, for both audiences."
    )
    applicant = WorkflowDescriptionApplicantSerializer()
    roles = WorkflowDescriptionRoleCellSerializer(many=True)
    notifications = WorkflowDescriptionRuleSerializer(many=True)
    extra = serializers.DictField(
        help_text="Fields of features described by extensions."
    )


class WorkflowDescriptionFlagSerializer(serializers.Serializer):
    code = serializers.CharField()
    kind = serializers.ChoiceField(
        choices=["missing", "duplicate", "unreachable", "leak_risk"]
    )
    severity = serializers.ChoiceField(choices=["warning", "info"])
    step = serializers.CharField(allow_null=True)
    trigger = serializers.CharField(allow_null=True)
    days_before = serializers.IntegerField(
        allow_null=True,
        help_text="The flagged rule's lead time, for a deadline reminder.",
    )
    recipient = serializers.CharField(allow_null=True)
    message = serializers.CharField()
    because = WorkflowDescriptionRefSerializer(many=True)


class WorkflowDescriptionAlwaysSentSerializer(serializers.Serializer):
    task = serializers.CharField()
    event = serializers.CharField()
    audience = serializers.ListField(child=serializers.CharField())
    mail_types = serializers.ListField(
        child=serializers.CharField(),
        help_text="Keys of the mail types a call may word for this mail.",
    )
    customised = serializers.BooleanField(
        help_text="Whether the call words one of them itself."
    )


class WorkflowDescriptionNotificationsSerializer(serializers.Serializer):
    flags = WorkflowDescriptionFlagSerializer(many=True)
    always_sent = WorkflowDescriptionAlwaysSentSerializer(many=True)
    customised = serializers.ListField(
        child=serializers.CharField(),
        help_text="Keys of the mail types the call words itself.",
    )


class WorkflowDescriptionStageSerializer(serializers.Serializer):
    stage = serializers.IntegerField()
    steps = serializers.ListField(child=serializers.CharField())
    start_day = serializers.IntegerField()
    end_day = serializers.IntegerField()


class WorkflowDescriptionPlannedStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    stage = serializers.IntegerField()
    start_day = serializers.IntegerField(
        help_text="Planned start, in days from the start of evaluation."
    )
    end_day = serializers.IntegerField(
        help_text="Planned end: the start plus the step's own duration."
    )
    conditional = serializers.BooleanField(
        help_text="The step has a run condition and is planned as if taken."
    )
    critical = serializers.BooleanField(
        help_text="The longest step of its stage, on the critical path."
    )


class WorkflowDescriptionPostAwardScheduleSerializer(serializers.Serializer):
    phase = serializers.ChoiceField(choices=WorkflowPhases.CHOICES)
    project_end_offset_days = serializers.IntegerField(allow_null=True)
    stages = WorkflowDescriptionStageSerializer(many=True)
    steps = WorkflowDescriptionPlannedStepSerializer(
        many=True, help_text="Planned in days from the start of the phase's run."
    )
    total_days = serializers.IntegerField()
    steps_without_duration = serializers.ListField(child=serializers.CharField())


class WorkflowDescriptionScheduleSerializer(serializers.Serializer):
    stages = WorkflowDescriptionStageSerializer(many=True)
    steps = WorkflowDescriptionPlannedStepSerializer(many=True)
    total_days = serializers.IntegerField()
    critical_path = serializers.ListField(child=serializers.CharField())
    steps_without_duration = serializers.ListField(child=serializers.CharField())
    post_award = WorkflowDescriptionPostAwardScheduleSerializer(
        many=True,
        help_text=(
            "The extension and the closing, each planned on its own after the "
            "award; the evaluation's schedule leaves them out."
        ),
    )


class WorkflowDescriptionRoundSerializer(serializers.Serializer):
    uuid = serializers.CharField(
        required=False, allow_null=True, help_text="The call's round; calls only."
    )
    name = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    start_time = serializers.DateTimeField(allow_null=True)
    cutoff_time = serializers.DateTimeField(allow_null=True)
    allocation_date = serializers.DateTimeField(allow_null=True)
    open_ended = serializers.BooleanField()
    review_duration_in_days = serializers.IntegerField(allow_null=True)
    time_to_decision_days = serializers.IntegerField(allow_null=True)
    time_to_decision_source = serializers.CharField(allow_null=True)


class WorkflowDescriptionCallSerializer(serializers.Serializer):
    name = serializers.CharField(allow_blank=True)
    state = serializers.CharField(allow_null=True)
    template = serializers.CharField(allow_null=True)
    settings = serializers.DictField()
    target_decision_days = serializers.IntegerField(allow_null=True)
    rounds = WorkflowDescriptionRoundSerializer(many=True)
    extra = serializers.DictField()


class WorkflowDescriptionSerializer(serializers.Serializer):
    schema = serializers.IntegerField()
    call = WorkflowDescriptionCallSerializer()
    actors = WorkflowDescriptionActorSerializer(many=True)
    steps = WorkflowDescriptionStepSerializer(many=True)
    notifications = WorkflowDescriptionNotificationsSerializer()
    schedule = WorkflowDescriptionScheduleSerializer()
    issues_structural = serializers.ListField(child=serializers.DictField())


# -- workflow check, versions and changes ---------------------------------------


class WorkflowCheckFindingSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="Stable identifier of the rule.")
    title = serializers.CharField(
        help_text="The rule's name in a few words, for a list of findings."
    )
    # Alphabetical: the schema names this set WorkflowCheckSeverity.
    severity = serializers.ChoiceField(choices=["error", "info", "warning"])
    step = serializers.CharField(
        allow_null=True, help_text="Key of the step; null for the call."
    )
    step_name = serializers.CharField(allow_null=True)
    field = serializers.CharField(
        allow_null=True,
        help_text=(
            "The setting that fixes it: a step field, or a call-level "
            "section such as rounds, offerings or panels."
        ),
    )
    message = serializers.CharField()
    because = WorkflowDescriptionRefSerializer(many=True)


class WorkflowCheckSummarySerializer(serializers.Serializer):
    error = serializers.IntegerField()
    warning = serializers.IntegerField()
    info = serializers.IntegerField()


class WorkflowCheckSerializer(serializers.Serializer):
    findings = WorkflowCheckFindingSerializer(many=True)
    summary = WorkflowCheckSummarySerializer()
    can_activate = serializers.BooleanField(
        help_text="Whether no finding is an error; warnings never block."
    )


class CallActivationRefusedSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="Every error, in one message.")
    workflow_check = WorkflowCheckFindingSerializer(many=True)


class WorkflowVersionSummarySerializer(serializers.Serializer):
    behaviour = serializers.IntegerField(required=False)
    cosmetic = serializers.IntegerField(required=False)
    steps_added = serializers.IntegerField(required=False)
    steps_removed = serializers.IntegerField(required=False)
    steps_changed = serializers.IntegerField(required=False)
    steps_moved = serializers.IntegerField(required=False)


class WorkflowVersionSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    summary = WorkflowVersionSummarySerializer(
        help_text="Counts of the changes from the previous version."
    )

    class Meta:
        model = models.WorkflowVersion
        fields = (
            "number",
            "created",
            "modified",
            "created_by_name",
            "reason",
            "summary",
            "document_hash",
        )

    def get_created_by_name(self, version) -> str | None:
        user = version.created_by
        return (user.full_name or user.username) if user else None


class WorkflowVersionDetailSerializer(WorkflowVersionSerializer):
    document = serializers.JSONField(
        help_text="The settings and workflow, in the call-transfer format."
    )

    class Meta(WorkflowVersionSerializer.Meta):
        fields = (*WorkflowVersionSerializer.Meta.fields, "document")


class WorkflowFieldChangeSerializer(serializers.Serializer):
    field = serializers.CharField()
    kind = serializers.ChoiceField(choices=["behaviour", "cosmetic"])
    old = serializers.JSONField(allow_null=True)
    new = serializers.JSONField(allow_null=True)
    added = serializers.ListField(child=serializers.JSONField())
    removed = serializers.ListField(child=serializers.JSONField())


class WorkflowEntityChangeSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    old_name = serializers.CharField(allow_null=True)
    change = serializers.ChoiceField(choices=["added", "removed", "changed", "moved"])
    kind = serializers.ChoiceField(choices=["behaviour", "cosmetic"])
    moved = serializers.BooleanField(
        help_text="Whether the step's place relative to the others changed."
    )
    fields = WorkflowFieldChangeSerializer(many=True)


class WorkflowChangesSerializer(serializers.Serializer):
    steps = WorkflowEntityChangeSerializer(many=True)
    call = WorkflowFieldChangeSerializer(many=True)
    proposal_roles = WorkflowEntityChangeSerializer(many=True)
    panels = WorkflowEntityChangeSerializer(many=True)
    forms = WorkflowEntityChangeSerializer(many=True)
    summary = WorkflowVersionSummarySerializer()


class WorkflowDiffTemplateSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(format="hex")
    name = serializers.CharField()
    version = serializers.IntegerField(
        allow_null=True, help_text="The version the call was created from."
    )
    latest_version = serializers.IntegerField()


class WorkflowDiffSerializer(serializers.Serializer):
    from_version = serializers.CharField()
    to_version = serializers.CharField()
    template = WorkflowDiffTemplateSerializer(allow_null=True)
    changes = WorkflowChangesSerializer()


# -- workflow observability ----------------------------------------------------
# Output-only serializers giving ``workflow_observability`` its schema.


class WorkflowProgressRoundSerializer(serializers.Serializer):
    uuid = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    start_time = serializers.DateTimeField(allow_null=True)
    cutoff_time = serializers.DateTimeField(allow_null=True)
    allocation_date = serializers.DateTimeField(allow_null=True)


class WorkflowProgressOutcomeSerializer(serializers.Serializer):
    outcome = serializers.CharField()
    count = serializers.IntegerField()
    share = serializers.FloatField(
        help_text="Of the step's completed instances, the fraction with this outcome."
    )


class WorkflowProgressRoundStepSerializer(serializers.Serializer):
    round = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    opened = serializers.BooleanField(
        help_text="Whether the call's managers opened the step for the round."
    )
    due_at = serializers.DateTimeField(allow_null=True)
    overdue = serializers.BooleanField(
        help_text="Past its due date in the round with proposals still at it."
    )
    waiting = serializers.IntegerField(help_text="The round's proposals at the step.")
    completed = serializers.IntegerField()
    reports = serializers.IntegerField(help_text="Round reports of the step.")
    reports_submitted = serializers.IntegerField()


class WorkflowProgressStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    enabled = serializers.BooleanField()
    phase = serializers.ChoiceField(choices=WorkflowPhases.CHOICES)
    round_scoped = serializers.BooleanField()
    rounds = WorkflowProgressRoundStepSerializer(
        many=True, help_text="Per round, for a step run once per round."
    )
    planned_days = serializers.IntegerField(allow_null=True)
    total = serializers.IntegerField()
    pending = serializers.IntegerField()
    waiting = serializers.IntegerField(help_text="Proposals at the step now.")
    overdue = serializers.IntegerField(
        help_text="Proposals at the step now and past its deadline."
    )
    completed = serializers.IntegerField()
    completed_late = serializers.IntegerField(
        help_text="Completed after the step's deadline."
    )
    skipped = serializers.IntegerField()
    expired = serializers.IntegerField()
    withdrawn = serializers.IntegerField()
    median_days = serializers.FloatField(
        allow_null=True, help_text="Median days from start to completion."
    )
    p90_days = serializers.FloatField(
        allow_null=True, help_text="90th percentile of days from start to completion."
    )
    outcomes = WorkflowProgressOutcomeSerializer(many=True)


class WorkflowProgressSerializer(serializers.Serializer):
    round = serializers.CharField(
        allow_null=True, help_text="The round counted; null for every round."
    )
    rounds = WorkflowProgressRoundSerializer(many=True)
    generated_at = serializers.DateTimeField()
    proposals = serializers.IntegerField(
        help_text="Proposals that have entered the workflow."
    )
    steps = WorkflowProgressStepSerializer(many=True)


class WorkflowTimelineStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    stage = serializers.IntegerField()
    status = serializers.CharField()
    outcome = serializers.CharField(allow_null=True)
    planned_days = serializers.IntegerField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)
    deadline = serializers.DateTimeField(allow_null=True)
    overdue = serializers.BooleanField(
        help_text="Past its deadline now, or completed after it."
    )
    completed_by_name = serializers.CharField(allow_null=True)
    sent_notifications = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "Keys of the mail the step's rules sent, without time: what went "
            "out before sends were logged as events."
        ),
    )


class WorkflowTimelineNotificationSerializer(serializers.Serializer):
    created = serializers.DateTimeField()
    step_key = serializers.CharField(allow_null=True)
    step_name = serializers.CharField(allow_null=True)
    template = serializers.CharField()
    trigger = serializers.CharField(allow_blank=True)
    audience = serializers.CharField(
        allow_blank=True, help_text="The role addressed; never an address."
    )
    recipient_count = serializers.IntegerField()
    email_log_uuids = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "The mails as sent, readable through the proposal's "
            "workflow-mails; empty for mail sent before they were kept."
        ),
    )


class WorkflowMailSerializer(serializers.Serializer):
    """A workflow mail as it went out."""

    uuid = serializers.UUIDField(format="hex")
    sent_at = serializers.DateTimeField()
    subject = serializers.CharField()
    body = serializers.CharField()
    emails = serializers.ListField(child=serializers.EmailField())


class WorkflowTimelineSerializer(serializers.Serializer):
    proposal_uuid = serializers.CharField()
    proposal_name = serializers.CharField()
    state = serializers.CharField()
    generated_at = serializers.DateTimeField()
    steps = WorkflowTimelineStepSerializer(many=True)
    notifications = WorkflowTimelineNotificationSerializer(many=True)


class WorkflowPathStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    result = serializers.CharField(
        help_text=(
            "The outcome of a completed step, otherwise its status: active, "
            "skipped, expired or withdrawn."
        )
    )


class WorkflowPathSerializer(serializers.Serializer):
    steps = WorkflowPathStepSerializer(many=True)
    proposals = serializers.IntegerField()
    finished = serializers.BooleanField(
        help_text="No proposal on the path is still at a step."
    )
    covered_by_scenario = serializers.BooleanField(
        allow_null=True,
        help_text="Whether a saved scenario walks the path; null when unknown.",
    )


class WorkflowPathsSerializer(serializers.Serializer):
    round = serializers.CharField(allow_null=True)
    scenarios_known = serializers.BooleanField(
        help_text="Whether the call has saved scenarios to compare against."
    )
    paths = WorkflowPathSerializer(many=True)


# =============================================================================
# Workflow dry run
# =============================================================================

DRY_RUN_MAX_ACTIONS = 200
DRY_RUN_MAX_OFFERINGS = 10
DRY_RUN_MAX_TEXT = 1000


class DryRunSlotSerializer(serializers.Serializer):
    """A piece of a step's work: by the index of the requested offering it is
    for, or by its place among the step's contributions."""

    offering = serializers.IntegerField(
        min_value=0,
        max_value=DRY_RUN_MAX_OFFERINGS - 1,
        required=False,
        allow_null=True,
    )
    slot = serializers.IntegerField(
        min_value=0, max_value=499, required=False, allow_null=True
    )

    def validate(self, attrs):
        if (attrs.get("offering") is None) == (attrs.get("slot") is None):
            raise serializers.ValidationError("Name either an offering or a slot.")
        return attrs


class DryRunContributionSerializer(DryRunSlotSerializer):
    outcome = serializers.CharField(max_length=40, required=False, allow_blank=True)
    conditions = serializers.CharField(
        max_length=DRY_RUN_MAX_TEXT, required=False, allow_blank=True
    )


class DryRunActionSerializer(serializers.Serializer):
    """One action of a dry run, taken on ``step``: an ``outcome`` (with the
    ``contributions`` that lead to it), ``contributions`` alone, or one of
    the flags."""

    FLAGS = (
        "reject",
        "expire",
        "request_changes",
        "submit_changes",
        "cancel_changes",
        "advance",
        "publish",
        "release",
        "round_step",
        "extend",
        "final_report",
        "exhaust_budget",
    )

    step = serializers.CharField(max_length=255, required=False, allow_blank=True)
    outcome = serializers.CharField(max_length=40, required=False, allow_blank=True)
    reason = serializers.CharField(
        max_length=DRY_RUN_MAX_TEXT, required=False, allow_blank=True
    )
    message = serializers.CharField(
        max_length=DRY_RUN_MAX_TEXT, required=False, allow_blank=True
    )
    contributions = DryRunContributionSerializer(
        many=True, required=False, max_length=50
    )
    parts = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, max_length=50
    )
    reject = serializers.BooleanField(required=False)
    expire = serializers.BooleanField(required=False)
    request_changes = serializers.BooleanField(required=False)
    submit_changes = serializers.BooleanField(required=False)
    cancel_changes = serializers.BooleanField(required=False)
    advance = serializers.BooleanField(required=False)
    publish = serializers.BooleanField(required=False)
    release = serializers.BooleanField(required=False)
    round_step = serializers.BooleanField(
        required=False,
        help_text=(
            "Open the step run once per round for the round, have its round "
            "reports written and complete it: with ``outcome`` for a decision."
        ),
    )
    extend = serializers.BooleanField(
        required=False,
        help_text="The applicant team asks to extend the accepted proposal.",
    )
    final_report = serializers.BooleanField(
        required=False, help_text="Open the accepted proposal's final report."
    )
    exhaust_budget = serializers.BooleanField(
        required=False,
        help_text=(
            "Take the budgets the proposal draws on as spent, so its award no "
            "longer fits."
        ),
    )
    override_budget = serializers.CharField(
        max_length=DRY_RUN_MAX_TEXT,
        required=False,
        allow_blank=True,
        help_text="With an outcome: approve past the budget, for this reason.",
    )
    scores = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=100),
        required=False,
        max_length=20,
        help_text=(
            "With an outcome on a step its assigned reviewers review: the "
            "score of each review, in order, one review per score; each "
            "passes the step's threshold by default."
        ),
    )
    delegate = DryRunSlotSerializer(required=False)
    reopen = DryRunSlotSerializer(required=False)

    def validate(self, attrs):
        chosen = [flag for flag in self.FLAGS if attrs.get(flag)]
        chosen += [name for name in ("delegate", "reopen") if attrs.get(name)]
        # A round step takes the outcome of its decision with it.
        if attrs.get("outcome") and not attrs.get("round_step"):
            chosen.append("outcome")
        if not chosen and attrs.get("contributions"):
            chosen.append("contributions")
        if attrs.get("scores") and not attrs.get("outcome"):
            raise serializers.ValidationError(
                {"scores": "Scores go with an outcome of the step."}
            )
        if len(chosen) != 1:
            raise serializers.ValidationError(
                "An action does exactly one thing: an outcome, contributions, "
                f"or one of {', '.join(self.FLAGS + ('delegate', 'reopen'))}."
            )
        return attrs


class DryRunViewAsSerializer(serializers.Serializer):
    role = serializers.CharField(
        max_length=255,
        help_text=(
            "An actor key of the call's workflow description, never a user: "
            "applicant, reviewer, offering_team, proposal_role:<key>, ..."
        ),
    )
    offering = serializers.IntegerField(
        min_value=0,
        max_value=DRY_RUN_MAX_OFFERINGS - 1,
        required=False,
        allow_null=True,
        help_text="For an offering team: the index of the requested offering.",
    )
    at = serializers.IntegerField(
        min_value=0,
        max_value=DRY_RUN_MAX_ACTIONS,
        required=False,
        allow_null=True,
        help_text="Preview after this many actions; all of them by default.",
    )


class WorkflowDryRunParametersSerializer(serializers.Serializer):
    requested_offerings = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        max_length=DRY_RUN_MAX_OFFERINGS,
        help_text=(
            "Requested offerings of the call, accepted ones, in the order "
            "actions name them; the call's first accepted ones by default."
        ),
    )
    actions = DryRunActionSerializer(
        many=True, required=False, max_length=DRY_RUN_MAX_ACTIONS
    )
    view_as = DryRunViewAsSerializer(required=False, allow_null=True)


class DryRunAudienceSerializer(serializers.Serializer):
    recipient = serializers.CharField()
    actor = serializers.CharField(allow_null=True)
    label = serializers.CharField()
    count = serializers.IntegerField()
    cc_count = serializers.IntegerField(
        required=False,
        help_text=(
            "How many were copied (Cc) on the message: on the applicant's "
            "mail, the holders of the proposal roles the call copies."
        ),
    )


class DryRunMessageSerializer(serializers.Serializer):
    index = serializers.IntegerField()
    action = serializers.IntegerField(allow_null=True)
    day = serializers.IntegerField()
    trigger = serializers.CharField(allow_blank=True)
    notification = serializers.CharField()
    enabled = serializers.BooleanField()
    step = serializers.CharField(allow_null=True)
    step_name = serializers.CharField(allow_null=True)
    audience = DryRunAudienceSerializer()
    single_message = serializers.BooleanField()
    subject = serializers.CharField(allow_blank=True)
    text = serializers.CharField(allow_blank=True)
    html = serializers.CharField(allow_blank=True)


class DryRunLogEntrySerializer(serializers.Serializer):
    index = serializers.IntegerField()
    action = serializers.IntegerField(allow_null=True)
    day = serializers.IntegerField()
    kind = serializers.CharField()
    step = serializers.CharField(allow_null=True)
    step_name = serializers.CharField(allow_null=True)
    text = serializers.CharField(allow_blank=True)
    outcome = serializers.CharField(allow_null=True, required=False)
    message = serializers.IntegerField(allow_null=True, required=False)


class DryRunContributionStateSerializer(serializers.Serializer):
    slot = serializers.IntegerField()
    offering = serializers.IntegerField(allow_null=True)
    label = serializers.CharField()
    status = serializers.CharField()
    outcome = serializers.CharField(allow_null=True)
    delegated = serializers.BooleanField()


class DryRunStepStateSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    order = serializers.IntegerField()
    stage = serializers.IntegerField()
    phase = serializers.ChoiceField(choices=WorkflowPhases.CHOICES)
    run = serializers.IntegerField(
        allow_null=True,
        help_text="Which extension request or final report, counted from 1.",
    )
    round_scoped = serializers.BooleanField()
    status = serializers.CharField()
    outcome = serializers.CharField(allow_null=True)
    skipped_reason = serializers.CharField(allow_null=True)
    condition = serializers.DictField(allow_null=True)
    started_day = serializers.IntegerField(allow_null=True)
    deadline_day = serializers.IntegerField(allow_null=True)
    ended_day = serializers.IntegerField(allow_null=True)
    is_allocation_decision = serializers.BooleanField()
    decides_automatically = serializers.BooleanField()
    automatic = serializers.BooleanField()
    released = serializers.BooleanField()
    changes_requested = serializers.BooleanField()
    outcomes = serializers.ListField(child=serializers.CharField())
    contribution_outcomes = serializers.ListField(child=serializers.CharField())
    contributions = DryRunContributionStateSerializer(many=True)
    extra = serializers.DictField()


class DryRunNextStepSerializer(serializers.Serializer):
    step = serializers.CharField()
    name = serializers.CharField()
    outcomes = serializers.ListField(child=serializers.CharField())
    contribution_outcomes = serializers.ListField(child=serializers.CharField())
    open_slots = serializers.ListField(child=serializers.IntegerField())
    can_reject = serializers.BooleanField()
    can_expire = serializers.BooleanField()
    can_request_changes = serializers.BooleanField()
    can_cancel_changes = serializers.BooleanField()
    can_delegate = serializers.BooleanField()
    can_reopen = serializers.ListField(child=serializers.IntegerField())
    can_release = serializers.BooleanField()
    round_step = serializers.BooleanField(
        help_text="Run once per round: taken with a round_step action."
    )


class DryRunNextSerializer(serializers.Serializer):
    steps = DryRunNextStepSerializer(many=True)
    can_submit_changes = serializers.BooleanField()
    can_advance = serializers.BooleanField()
    can_publish = serializers.BooleanField()
    can_extend = serializers.BooleanField()
    can_open_final_report = serializers.BooleanField()
    can_exhaust_budget = serializers.BooleanField()


class DryRunOutcomeCoverageSerializer(serializers.Serializer):
    available = serializers.ListField(child=serializers.CharField())
    tried = serializers.ListField(child=serializers.CharField())


class DryRunCoverageSerializer(serializers.Serializer):
    steps = serializers.ListField(child=serializers.CharField())
    exercised = serializers.ListField(child=serializers.CharField())
    outcomes = serializers.DictField(child=DryRunOutcomeCoverageSerializer())
    contribution_outcomes = serializers.DictField(
        child=DryRunOutcomeCoverageSerializer()
    )


class DryRunRequestedOfferingSerializer(serializers.Serializer):
    index = serializers.IntegerField()
    uuid = serializers.UUIDField()
    name = serializers.CharField()


class DryRunViewerSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()
    is_staff = serializers.BooleanField()
    is_support = serializers.BooleanField()
    permissions = serializers.ListField(child=serializers.DictField())


class DryRunReadSerializer(serializers.Serializer):
    status = serializers.IntegerField()
    data = serializers.JSONField(allow_null=True)


class DryRunViewAsResultSerializer(serializers.Serializer):
    role = serializers.CharField()
    offering = serializers.IntegerField(allow_null=True)
    at = serializers.IntegerField(allow_null=True)
    viewer = DryRunViewerSerializer()
    described_as = serializers.CharField()
    proposal_uuid = serializers.UUIDField()
    responses = serializers.DictField(child=DryRunReadSerializer())


class ProposalPreviewRoleSerializer(serializers.Serializer):
    role = serializers.CharField(help_text="An actor key of the call's workflow.")
    offering = serializers.IntegerField(
        allow_null=True,
        help_text="For an offering's team: the index of its requested offering.",
    )
    label = serializers.CharField()


class ProposalPreviewAsSerializer(serializers.Serializer):
    role = serializers.CharField(
        max_length=255,
        help_text="One of the proposal's preview-roles: an actor key, never a user.",
    )
    offering = serializers.IntegerField(
        min_value=0,
        max_value=DRY_RUN_MAX_OFFERINGS - 1,
        required=False,
        allow_null=True,
        help_text="For an offering's team: the index of its requested offering.",
    )


class WorkflowDryRunSerializer(serializers.Serializer):
    proposal_state = serializers.CharField()
    decision_held = serializers.BooleanField()
    day = serializers.IntegerField()
    requested_offerings = DryRunRequestedOfferingSerializer(many=True)
    steps = DryRunStepStateSerializer(many=True)
    next = DryRunNextSerializer()
    log = DryRunLogEntrySerializer(many=True)
    messages = DryRunMessageSerializer(many=True)
    coverage = DryRunCoverageSerializer()
    view_as = DryRunViewAsResultSerializer(allow_null=True)
    scenario_draft = serializers.JSONField(
        required=False,
        help_text=(
            "The path as a scenario: its request and assertions on where it "
            "ended, to name and save on the call's template."
        ),
    )


# =============================================================================
# Saved scenarios
# =============================================================================

SCENARIO_MAX = 20
ASSERTION_MAX = 60
CHECK_TEXT_MAX = 2000
CHECK_KEY_MAX = 150

STEP_STATUSES = [status for status, _ in WorkflowStepInstanceStatuses.CHOICES]
PROPOSAL_STATE_VALUES = [state for state, _ in ProposalStates.CHOICES]
LABEL_AUDIENCES = list(step_status_labels.AUDIENCES)


def _key(**kwargs):
    return serializers.CharField(max_length=CHECK_KEY_MAX, **kwargs)


class _MailFilterSerializer(serializers.Serializer):
    step = _key(required=False, allow_blank=True, allow_null=True)
    trigger = _key(required=False, allow_blank=True, allow_null=True)
    actor = _key(required=False, allow_blank=True, allow_null=True)
    recipient = _key(required=False, allow_blank=True, allow_null=True)


class _NotifiedAssertionSerializer(_MailFilterSerializer):
    count = serializers.IntegerField(min_value=0, max_value=1000, required=False)
    min = serializers.IntegerField(min_value=0, max_value=1000, required=False)


class _StepStatusAssertionSerializer(serializers.Serializer):
    step = _key()
    status = serializers.ChoiceField(choices=STEP_STATUSES)
    outcome = _key(required=False, allow_null=True, allow_blank=True)


class _LabelAssertionSerializer(serializers.Serializer):
    step = _key()
    audience = serializers.ChoiceField(choices=LABEL_AUDIENCES, required=False)
    role = _key(
        required=False,
        help_text=(
            "Read the label off the dashboard of this role of the workflow, "
            "through the real endpoint, rather than off the configuration."
        ),
    )
    text = serializers.CharField(max_length=255, allow_null=True)

    def validate(self, attrs):
        if ("audience" in attrs) == ("role" in attrs):
            raise serializers.ValidationError("Name either an audience or a role.")
        return attrs


class _ViewAssertionSerializer(serializers.Serializer):
    role = _key()
    step = _key()


def _one_kind(value, kinds, what):
    if not isinstance(value, dict):
        raise serializers.ValidationError(f"Each {what} is an object.")
    named = [kind for kind in kinds if kind in value]
    if len(named) != 1:
        raise serializers.ValidationError(
            f"Each {what} names exactly one of: {', '.join(kinds)}."
        )
    return named[0]


def _nested(serializer_class, value, kind):
    serializer = serializer_class(data=value)
    if not serializer.is_valid():
        raise serializers.ValidationError({kind: serializer.errors})
    return dict(serializer.validated_data)


def _text(value, kind, max_length=CHECK_TEXT_MAX):
    if not isinstance(value, str) or not value.strip():
        raise serializers.ValidationError({kind: "A non-empty text."})
    if len(value) > max_length:
        raise serializers.ValidationError({kind: f"At most {max_length} characters."})
    return value


class ScenarioAssertionField(serializers.JSONField):
    """One assertion of a scenario over the dry run's end: the
    ``proposal_state``, a ``step_status``, mail ``notified`` (with a ``count``
    or ``min``) or ``not_notified``, the ``label`` an audience's dashboard
    shows, a step ``visible`` or ``hidden`` to a role, or the step the
    workflow ``ends_at``."""

    KINDS = (
        "proposal_state",
        "step_status",
        "notified",
        "not_notified",
        "label",
        "visible",
        "hidden",
        "ends_at",
    )

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        kind = _one_kind(value, self.KINDS, "assertion")
        if set(value) - {kind}:
            raise serializers.ValidationError(
                f"An assertion holds its '{kind}' and nothing else."
            )
        if kind == "proposal_state":
            if value[kind] not in PROPOSAL_STATE_VALUES:
                raise serializers.ValidationError(
                    {kind: f"One of: {', '.join(PROPOSAL_STATE_VALUES)}."}
                )
        elif kind == "step_status":
            value[kind] = _nested(_StepStatusAssertionSerializer, value[kind], kind)
        elif kind == "notified":
            value[kind] = _nested(_NotifiedAssertionSerializer, value[kind], kind)
        elif kind == "not_notified":
            value[kind] = _nested(_MailFilterSerializer, value[kind], kind)
        elif kind == "label":
            value[kind] = _nested(_LabelAssertionSerializer, value[kind], kind)
        elif kind in ("visible", "hidden"):
            value[kind] = _nested(_ViewAssertionSerializer, value[kind], kind)
        else:
            _text(value[kind], kind, CHECK_KEY_MAX)
        return value


class ScenarioRequestSerializer(serializers.Serializer):
    offerings = serializers.IntegerField(
        min_value=0,
        max_value=DRY_RUN_MAX_OFFERINGS,
        required=False,
        default=1,
        help_text=(
            "How many offerings the proposal requests: a call's first "
            "accepted ones, or a template's synthetic ones."
        ),
    )
    actions = DryRunActionSerializer(
        many=True, required=False, max_length=DRY_RUN_MAX_ACTIONS
    )


class CallTemplateScenarioSerializer(serializers.Serializer):
    key = serializers.SlugField(max_length=64)
    name = serializers.CharField(max_length=CHECK_KEY_MAX)
    description = serializers.CharField(
        max_length=CHECK_TEXT_MAX, required=False, allow_blank=True
    )
    request = ScenarioRequestSerializer()
    assertions = serializers.ListField(
        child=ScenarioAssertionField(), min_length=1, max_length=ASSERTION_MAX
    )

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        # Plain JSON, each action with only what it sets.
        return json.loads(json.dumps(value))


def _unique(items, name, what):
    seen = set()
    for item in items:
        if item[name] in seen:
            raise serializers.ValidationError(
                f"Two {what} share the {name} '{item[name]}'."
            )
        seen.add(item[name])


def validate_template_scenarios(value):
    if not isinstance(value, list):
        raise serializers.ValidationError("A list of scenarios.")
    if len(value) > SCENARIO_MAX:
        raise serializers.ValidationError(f"At most {SCENARIO_MAX} scenarios.")
    serializer = CallTemplateScenarioSerializer(data=value, many=True)
    serializer.is_valid(raise_exception=True)
    scenarios = [dict(item) for item in serializer.validated_data]
    _unique(scenarios, "key", "scenarios")
    return scenarios


class ScenarioAssertionResultSerializer(serializers.Serializer):
    assertion = serializers.JSONField()
    passed = serializers.BooleanField()
    actual = serializers.JSONField(allow_null=True)
    step = serializers.CharField(allow_null=True)
    message = serializers.CharField(allow_blank=True)


class ScenarioResultSerializer(serializers.Serializer):
    key = serializers.CharField(allow_null=True)
    name = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=["passed", "failed", "error"])
    error = serializers.CharField(allow_null=True)
    action = serializers.IntegerField(
        allow_null=True, help_text="Index of the action the run stopped at."
    )
    failure = ScenarioAssertionResultSerializer(allow_null=True)
    assertions = ScenarioAssertionResultSerializer(many=True)
    path = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text="The (step, outcome or status) pairs the proposal took.",
    )
    proposal_state = serializers.CharField(allow_null=True)
    exercised = serializers.ListField(child=serializers.CharField())


class ScenarioCoverageSerializer(serializers.Serializer):
    steps = serializers.IntegerField()
    exercised = serializers.ListField(child=serializers.CharField())
    unexercised = serializers.ListField(child=serializers.CharField())
    untried_outcomes = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField())
    )


class CallTemplateReferenceSerializer(serializers.Serializer):
    uuid = serializers.CharField()
    name = serializers.CharField()
    version = serializers.IntegerField(
        allow_null=True, help_text="The template's version the call started from."
    )


class CallScenarioSummarySerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)


class CallWorkflowScenariosSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=CheckRunStatuses.CHOICES,
        help_text=(
            "'running' while a run asked for with run-workflow-scenarios has "
            "not finished."
        ),
    )
    requested_at = serializers.CharField(
        allow_null=True, help_text="When the latest run was asked for."
    )
    error = serializers.CharField(
        allow_null=True,
        help_text=(
            "Why the latest run's results were not kept: the earlier results "
            "stay in place."
        ),
    )
    template = CallTemplateReferenceSerializer(allow_null=True)
    scenarios = CallScenarioSummarySerializer(many=True)
    version = serializers.IntegerField(allow_null=True)
    checked_at = serializers.CharField(allow_null=True)
    results = ScenarioResultSerializer(many=True)
    totals = ScenarioTotalsSerializer(allow_null=True)
    coverage = ScenarioCoverageSerializer(allow_null=True)


# -- a call's own wording of its mail -------------------------------------------


class CallNotificationOverrideSerializer(serializers.ModelSerializer):
    """A call's own wording of one mail type. Validated by rendering it in
    the sandbox against an example context of its mail type."""

    updated_by_name = serializers.SerializerMethodField()
    key = serializers.SerializerMethodField()

    class Meta:
        model = models.CallNotificationOverride
        fields = (
            "uuid",
            "key",
            "mail_type",
            "variant",
            "subject",
            "body_text",
            "body_html",
            "enabled",
            "updated_by_name",
            "modified",
        )
        read_only_fields = ("uuid", "key", "updated_by_name", "modified")
        extra_kwargs = {
            "subject": {"max_length": notification_overrides.MAX_SUBJECT},
            "body_text": {"max_length": notification_overrides.MAX_BODY_TEXT},
            "body_html": {
                "max_length": notification_overrides.MAX_BODY_HTML,
                "required": False,
            },
            "variant": {"required": False},
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_updated_by_name(self, obj) -> str | None:
        user = obj.updated_by
        return (user.full_name or user.username) if user else None

    def get_key(self, obj) -> str:
        return notification_overrides.mail_key(obj.mail_type, obj.variant)

    def validate(self, attrs):
        instance = self.instance
        mail_type = attrs.get("mail_type", instance.mail_type if instance else None)
        variant = attrs.get("variant", instance.variant if instance else "") or ""
        definition = notification_overrides.get_type(mail_type, variant)
        if definition is None:
            raise serializers.ValidationError({"mail_type": "Not a mail a call sends."})
        if instance is not None and (
            mail_type != instance.mail_type or variant != instance.variant
        ):
            raise serializers.ValidationError(
                {"mail_type": "The mail type of a wording cannot be changed."}
            )
        try:
            notification_overrides.validate(
                definition,
                attrs.get("subject", instance.subject if instance else ""),
                attrs.get("body_text", instance.body_text if instance else ""),
                attrs.get("body_html", instance.body_html if instance else ""),
            )
        except notification_overrides.OverrideError as error:
            raise serializers.ValidationError(error.errors)
        attrs["variant"] = variant
        return attrs


class NotificationVariableSerializer(serializers.Serializer):
    name = serializers.CharField()
    description = serializers.CharField()


class NotificationWordingSerializer(serializers.Serializer):
    subject = serializers.CharField(allow_blank=True)
    body_text = serializers.CharField(allow_blank=True)
    body_html = serializers.CharField(allow_blank=True)


class CallNotificationMailTypeSerializer(serializers.Serializer):
    key = serializers.CharField()
    mail_type = serializers.CharField()
    variant = serializers.CharField(allow_blank=True)
    label = serializers.CharField()
    audience = serializers.CharField()
    variables = NotificationVariableSerializer(many=True)
    portal_wording = NotificationWordingSerializer(
        help_text="The portal's wording: where the call's own wording starts from."
    )
    portal_overridden = serializers.BooleanField(
        help_text="Whether staff reworded this mail for the whole portal."
    )
    override = CallNotificationOverrideSerializer(allow_null=True)


class NotificationPreviewParametersSerializer(serializers.Serializer):
    step = serializers.CharField(required=False, allow_blank=True)
    trigger = serializers.ChoiceField(
        choices=NotificationRuleTriggers.CHOICES, required=False
    )
    recipient = serializers.ChoiceField(
        choices=NotificationRuleRecipients.CHOICES, required=False
    )
    days_before = serializers.IntegerField(required=False, min_value=0, max_value=365)
    mail_type = serializers.CharField(required=False, allow_blank=True)
    variant = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("step") and not attrs.get("mail_type"):
            raise serializers.ValidationError(
                "Name a step, a trigger and a recipient, or a mail type."
            )
        return attrs


class NotificationPreviewDraftSerializer(NotificationPreviewParametersSerializer):
    subject = serializers.CharField(
        allow_blank=True, max_length=notification_overrides.MAX_SUBJECT
    )
    body_text = serializers.CharField(
        allow_blank=True, max_length=notification_overrides.MAX_BODY_TEXT
    )
    body_html = serializers.CharField(
        allow_blank=True,
        required=False,
        default="",
        max_length=notification_overrides.MAX_BODY_HTML,
    )


class NotificationPreviewSerializer(serializers.Serializer):
    key = serializers.CharField()
    mail_type = serializers.CharField()
    variant = serializers.CharField(allow_blank=True)
    label = serializers.CharField()
    audience = serializers.CharField()
    notification = serializers.CharField()
    enabled = serializers.BooleanField(
        help_text="Whether the portal sends this notification at all."
    )
    source = serializers.ChoiceField(
        choices=["call", "portal", "default"],
        help_text="Whose wording: the call's own, the portal's, or Waldur's.",
    )
    step = serializers.CharField(allow_null=True)
    trigger = serializers.CharField(allow_null=True)
    recipient = serializers.CharField(allow_null=True)
    subject = serializers.CharField()
    text = serializers.CharField(allow_blank=True)
    html = serializers.CharField(allow_blank=True)


class OfferingDemandPoolSerializer(serializers.Serializer):
    """A resource pool of the call on the component: what it offers, what
    the awards held have taken of it and what remains. Counted as against a
    pool, in the component's measured unit over the whole length of each
    award, from every proposal the pool covers."""

    offered = LimitValueField()
    awarded = LimitValueField()
    remaining = LimitValueField()


class OfferingDemandComponentSerializer(serializers.Serializer):
    type = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    measured_unit = serializers.CharField(allow_blank=True)
    limit_period = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The period the component is limited per: requested and awarded "
            "sum the amounts per this period, as stored. 'total' is one "
            "amount for the whole allocation."
        ),
    )
    requested = LimitValueField(
        help_text="Summed over the requested resources of the row's proposals."
    )
    awarded = LimitValueField(
        allow_null=True,
        help_text=(
            "Summed over the awarded resources of the row's proposals; "
            "accepted rows only."
        ),
    )
    call_pool = OfferingDemandPoolSerializer(
        allow_null=True, help_text="The call-wide pool, if the call sets one."
    )
    round_pool = OfferingDemandPoolSerializer(
        allow_null=True, help_text="The round's pool, if the call sets one."
    )


class OfferingDemandSerializer(serializers.Serializer):
    """What the proposals of one round of a call in one group of states ask
    of one of the provider's offerings."""

    offering_uuid = serializers.UUIDField(source="offering.uuid", format="hex")
    offering_name = serializers.CharField(source="offering.name")
    provider_uuid = serializers.UUIDField(source="provider.uuid", format="hex")
    provider_name = serializers.CharField(source="provider.name")
    requested_offering_uuid = serializers.UUIDField(
        source="requested_offering.uuid", format="hex"
    )
    call_uuid = serializers.UUIDField(source="call.uuid", format="hex")
    call_name = serializers.CharField(source="call.name")
    call_state = serializers.ChoiceField(
        source="call.state", choices=CallStates.CHOICES
    )
    round_uuid = serializers.UUIDField(source="round.uuid", format="hex")
    round_name = serializers.CharField(source="round.name")
    round_start_time = serializers.DateTimeField(source="round.start_time")
    round_cutoff_time = serializers.DateTimeField(
        source="round.cutoff_time", allow_null=True
    )
    round_status = serializers.ChoiceField(
        source="round.status", choices=RoundStatuses.CHOICES
    )
    state_group = serializers.ChoiceField(
        choices=offering_demand.DemandGroups.CHOICES,
        help_text=(
            "pending: submitted or in review; accepted; closed: rejected or "
            "canceled. Drafts are not counted."
        ),
    )
    proposals_count = serializers.IntegerField()
    components = OfferingDemandComponentSerializer(
        many=True, help_text="Empty for closed rows, which only count proposals."
    )
