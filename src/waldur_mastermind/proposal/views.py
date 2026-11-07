import json
import logging
import secrets
import uuid as uuid_module
from collections import Counter
from datetime import datetime, timedelta
from typing import cast

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import (
    BooleanField,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Prefetch,
    ProtectedError,
    Q,
    Subquery,
)
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone as timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import decorators, exceptions, mixins, response, status, viewsets
from rest_framework import permissions as rf_permissions
from rest_framework.throttling import UserRateThrottle

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.mixins import ReviewerChecklistMixin, UserChecklistMixin
from waldur_core.configuration import config
from waldur_core.core import access
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import serializers as core_serializers
from waldur_core.core import validators as core_validators
from waldur_core.core.enums import ReviewStates as CoreReviewStates
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.models import User
from waldur_core.core.permissions import has_staff_access, has_support_access
from waldur_core.core.utils import SubqueryCount
from waldur_core.core.views import (
    ActionMethodMixin,
    ActionsViewSet,
    ReadOnlyActionsViewSet,
    no_count_action,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.permissions.utils import permission_factory
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers,
)
from waldur_core.structure.models import Customer
from waldur_core.structure.permissions import _get_customer
from waldur_core.user_actions.providers import DASHBOARD_LIST_LIMIT
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.views import (
    PUBLIC_CATALOG_ACCESS,
    BaseMarketplaceView,
    PublicViewsetMixin,
)
from waldur_mastermind.proposal import (
    activity,
    affinity_scoring,
    applicant_capacity,
    budgets,
    call_templates,
    call_transfer,
    change_requests,
    coi_detection,
    contributions,
    dry_run,
    exports,
    filters,
    models,
    my_tasks,
    notification_overrides,
    notification_preview,
    notification_rules,
    offering_demand,
    orcid_service,
    panels,
    proposal_roles,
    provider_calls,
    rankings,
    reports,
    reviewer_pool,
    reviewer_registry,
    reviewer_standing,
    reviewer_stats,
    reviewer_suggestions,
    round_lifecycle,
    round_reports,
    round_steps,
    runs,
    serializers,
    step_inputs,
    tasks,
    utils,
    workflow_checks,
    workflow_description,
    workflow_diff,
    workflow_observability,
    workflow_report,
    workflow_scenarios,
    workflow_service,
    workflow_versions,
)
from waldur_mastermind.proposal import enums as proposal_enums
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    AffinityMatrixScopes,
    CallStates,
    ChangeRequestParts,
    COIDetectionJobStates,
    COIDetectionJobTypes,
    COIDetectionMethods,
    COIStatuses,
    COITypes,
    EvaluationStart,
    ProposalFieldStates,
    ProposalRunStates,
    ProposalStates,
    RequestedOfferingStates,
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
    ReviewStates,
    RoundStatuses,
    StepKinds,
    TransitionModes,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.permissions import CALL_PERMISSION_SOURCES

from .managers import (
    assignment_batch_status_q,
    filter_reviews_for_user,
    get_connected_call_organizers,
    get_connected_calls,
    holds_reviewer_work,
    open_assignment_items_q,
    round_not_ended_q,
    sees_every_call,
)
from .models import Proposal
from .serializers import ReviewSubmitSerializer

logger = logging.getLogger(__name__)


HELD_DECISION_DETAIL = (
    "Decision recorded. It is announced when the round's results are published."
)


def validate_round_is_open(proposal):
    """A proposal may only be submitted while its round is open.

    Same rule as creation (``ProposalSerializer.validate``), so a proposal can
    never be created into a state it could not then be sent from.

    Nothing checked the round here before, so a draft could be submitted after
    the cutoff: that notified the call managers, created the workflow step
    instances and moved a late proposal into review. The deadline is enforced
    here, at the door: ``proposals_for_ended_rounds_should_be_cancelled`` only
    sweeps up the drafts left behind, never a proposal that was sent.
    """
    round_status = proposal.round.status
    if round_status == RoundStatuses.SCHEDULED:
        raise exceptions.ValidationError(
            _("Round has not opened yet, so the proposal cannot be submitted.")
        )
    if round_status == RoundStatuses.ENDED:
        raise exceptions.ValidationError(
            _("Round has closed, so the proposal can no longer be submitted.")
        )


def validate_call_is_active(proposal):
    """A proposal may only be submitted while its call is active.

    Same rule as creation. Archiving a call leaves its drafts behind, and a
    round's own dates say nothing about the call, so a draft in a round that is
    still open would otherwise be sent into an archived call.
    """
    if proposal.round.call.state != CallStates.ACTIVE:
        raise exceptions.ValidationError(models.CALL_NOT_ACTIVE_MESSAGE)


def check_reviewer_workload_limit(pool_entry, new_assignments, override, user):
    """Refuse assignments that would take a reviewer above their limit.

    ``pool_entry`` must be locked by the caller so that concurrent assignments
    to the same reviewer are counted one after the other. A call manager may
    pass ``override_workload_limit`` to assign anyway; the override is
    recorded as an event on the call.
    """
    if new_assignments <= 0:
        return
    open_assignments = pool_entry.get_open_assignments()
    if open_assignments + new_assignments <= pool_entry.max_assignments:
        return

    reviewer_name = (
        pool_entry.reviewer.user.full_name
        if pool_entry.reviewer
        else pool_entry.invited_email
    )
    params = {
        "reviewer": reviewer_name,
        "open": open_assignments,
        "limit": pool_entry.max_assignments,
        "new": new_assignments,
    }
    if not override:
        raise exceptions.ValidationError(
            _(
                "Reviewer %(reviewer)s has %(open)s open assignments and a limit "
                "of %(limit)s, so %(new)s more would exceed it. Set "
                "override_workload_limit to assign anyway."
            )
            % params
        )

    call = pool_entry.call
    event_logger.emit(
        f"{user.full_name or user.username} assigned {new_assignments} more "
        f"proposal(s) to reviewer {reviewer_name} in call {call.name}, above "
        f"the limit of {pool_entry.max_assignments} "
        f"({open_assignments} already open).",
        event_type=EventType.REVIEWER_WORKLOAD_LIMIT_OVERRIDDEN,
        event_context={"call": call},
        scopes=[_get_customer(call)],
    )


def _is_uuid(raw) -> bool:
    try:
        uuid_module.UUID(raw)
    except (TypeError, ValueError):
        return False
    return True


def _document_name(document) -> str:
    """The name a proposal document is told by: its file's own name."""
    name = document.file.name if document.file else ""
    return name.rsplit("/", 1)[-1] or "a document"


def validate_project_details_complete(proposal, fields=None):
    """Every field the call marked required carries a value.

    Until now requiredness lived only in the frontend, which disabled the submit
    button while a step reported itself incomplete — an API client could submit
    a proposal with an empty summary. A per-call configuration that only the
    form respected would be no stronger, so the rule is enforced here.

    Hidden and optional fields are never checked: the applicant was either not
    asked, or asked without obligation. ``fields``, when given, narrows the
    check to those fields: the ones a request for changes unlocked.
    """
    states = models.CallProposalFieldConfig.get_states_for_call(proposal.round.call)
    missing = []
    for field_name, state in states.items():
        if state != ProposalFieldStates.REQUIRED:
            continue
        if fields is not None and field_name not in fields:
            continue
        if field_name == "supporting_documentation":
            if not proposal.proposaldocumentation_set.exists():
                missing.append(field_name)
            continue
        if not getattr(proposal, field_name, None):
            missing.append(field_name)
    if missing:
        raise exceptions.ValidationError(
            _("The following required fields are empty: %(fields)s.")
            % {"fields": ", ".join(sorted(missing))}
        )


def validate_purchase_orders_present(proposal):
    """Every requested resource whose call entry demands a purchase order has one.

    Enforced at submission rather than at creation so an applicant can assemble
    the request first and attach the authorisation last, which is the order the
    two usually arrive in.

    The requirement is a property of the call entry
    (``RequestedOffering.require_purchase_order``), seeded from the offering's
    ``require_purchase_order_upload`` but owned by the call manager afterwards —
    the offering flag alone gates *order approval*, which happens well after a
    proposal is reviewed.

    The rule itself lives on the model so ``Proposal.can_submit`` reports
    exactly what this enforces, and the form can say what is missing rather
    than letting the applicant discover it from a rejected request.
    """
    missing = proposal.offerings_missing_purchase_orders()
    if missing:
        raise exceptions.ValidationError(
            _(
                "A purchase order is required for the following offerings: %(offerings)s."
            )
            % {"offerings": ", ".join(missing)}
        )


def validate_requested_duration(proposal):
    """A call that offers a list of durations is submitted with one of them."""
    error = proposal.requested_duration_error()
    if error:
        raise exceptions.ValidationError({"requested_duration_months": error})


def validate_resource_templates_used(proposal):
    """On a call with resource templates, the requests follow them."""
    error = proposal.resource_template_error()
    if error:
        raise exceptions.ValidationError(error)


def validate_proposal_manager_present(proposal):
    """A proposal is submitted with at least one proposal manager."""
    if not proposal.has_proposal_manager():
        raise exceptions.ValidationError(models.MISSING_PROPOSAL_MANAGER_MESSAGE)


def validate_resources_requested_for_offering_steps(proposal):
    """A call whose workflow works per requested offering needs at least one.

    Such a step opens one contribution slot per offering the proposal asks
    for, and is completed by those offerings' teams. Submitted asking for
    nothing, it would open no slot and name nobody to complete it, and the
    proposal would wait at it for good. Refused here, with the other checks
    of what a submission must carry, rather than letting the step pass with
    nothing assessed.
    """
    if proposal.requestedresource_set.exists():
        return
    if proposal.round.call.workflow_steps.filter(
        is_enabled=True, per_requested_offering=True
    ).exists():
        raise exceptions.ValidationError(
            _("Request at least one resource: this call assesses each one requested.")
        )


def validate_requested_amounts_present(proposal):
    """No requested resource may ask for an offering without naming an amount.

    Attaching an offering creates a resource request with empty limits, which
    the proposal form counted as a completed step. Submitted like that,
    ``allocate_proposal`` provisions a resource with no quota at all — the
    applicant is awarded nothing and finds out after the review.

    Offerings with nothing to ask for (no limit or prepaid component) are
    exempt: there is no amount to name in the first place.
    """
    missing = proposal.offerings_missing_requested_amounts()
    if missing:
        raise exceptions.ValidationError(
            _(
                "Requested amounts are missing for the following offerings: %(offerings)s."
            )
            % {"offerings": ", ".join(missing)}
        )


def validate_requests_within_budgets(proposal):
    """What the proposal requests lies within its offerings' component
    rules, summed per offering component."""
    messages = budgets.request_out_of_bounds(proposal)
    if messages:
        raise exceptions.ValidationError(messages)


def validate_required_options_present(proposal):
    """Every required option of a requested offering carries a value.

    A draft may leave them for later; the order allocation creates from the
    request takes them as they are, so they are asked for here.
    """
    missing = proposal.offerings_missing_required_options()
    if missing:
        raise exceptions.ValidationError(
            models.REQUIRED_OPTIONS_MISSING_MESSAGE % {"offerings": ", ".join(missing)}
        )


def validate_call_not_archived(nested_obj):
    """Reject update/delete of a nested object whose parent call is archived.

    Used as an ``action_detail_method`` validator: it receives the nested
    object (RequestedOffering / CallResourceTemplate / CallWorkflowStep /
    ProposalProjectRoleMapping), all of which reach their call via ``.call``.
    An archived call is read-only across its whole edit surface.
    """
    if nested_obj.call.state == CallStates.ARCHIVED:
        raise IncorrectStateException()


def validate_no_dependents(requested_offering):
    """Refuse to delete a call offering the call has set things on: they
    would silently go with it."""
    dependents = budgets.offering_dependents(requested_offering)
    if dependents:
        raise IncorrectStateException(
            _(
                "The offering has %(dependents)s. Remove them before removing "
                "the offering."
            )
            % {"dependents": "; ".join(str(d) for d in dependents)}
        )


def validate_step_not_walked(call_step):
    """Refuse to delete a call step a proposal has an instance of.

    The proposal's history points at it; the step is disabled instead. Nor
    can an active call lose its allocation decision.
    """
    if call_step.proposal_instances.exists():
        raise exceptions.ValidationError(
            _(
                "Proposals have already been submitted with this step; "
                "disable it instead."
            )
        )
    if (
        call_step.call.state == CallStates.ACTIVE
        and call_step.is_allocation_decision
        and call_step.is_enabled
    ):
        raise exceptions.ValidationError(
            _("An active call needs its allocation decision step.")
        )
    # A response step relies on a decision after it.
    try:
        models.CallWorkflowStep.check_responses_decided(
            call_step.call_id, removed_pk=call_step.pk
        )
    except DjangoValidationError as exc:
        raise exceptions.ValidationError(exc.message_dict["input_steps"])
    # The step assigning a role first may not leave a later one to assign it
    # after the steps its holders work on.
    try:
        models.CallWorkflowStep.check_roles_assigned_first(
            call_step.call_id, removed_pk=call_step.pk
        )
    except DjangoValidationError as exc:
        raise exceptions.ValidationError(exc.message_dict["order"])
    # Nor may a step run on its outcome, or a stage come together without it
    # that cannot run together.
    try:
        models.CallWorkflowStep.check_stages_and_conditions(
            call_step.call_id, removed_pk=call_step.pk
        )
    except DjangoValidationError as exc:
        raise exceptions.ValidationError(
            [message for messages in exc.message_dict.values() for message in messages]
        )


def validate_proposal_role_unused(role):
    """Refuse to delete an evaluator role in use.

    Assignments made in it are proposals' history, and steps and notification
    rules that name it would lose their meaning.
    """
    if proposal_roles.role_is_assigned(role):
        raise exceptions.ValidationError(
            _("The role has been assigned on proposals; it cannot be deleted.")
        )
    if role.assigning_steps.exists() or role.completed_steps.exists():
        raise exceptions.ValidationError(
            _("A workflow step uses this role; change the step first.")
        )
    if role.notification_rules.exists():
        raise exceptions.ValidationError(
            _("A notification rule addresses this role; remove the rule first.")
        )


def validate_panel_unused(panel):
    """Refuse to delete a panel proposals are routed to: move them first."""
    if panel.proposals.exists():
        raise exceptions.ValidationError(
            _(
                "Proposals are routed to this panel; move them to another "
                "panel before deleting it."
            )
        )


def validate_compliance_answered(proposal):
    """A call's compliance checklist is part of the application: its
    required questions are answered before the proposal is submitted."""
    unanswered = proposal.unanswered_compliance_questions()
    if unanswered:
        raise exceptions.ValidationError(
            models.unanswered_compliance_message(unanswered)
        )


def validate_submitted_changes(proposal, parts):
    """What submitting the changes asked of the applicant checks: what a
    submission must carry, but the round's cut-off (the proposal was
    submitted in time), and only in the ``parts`` the open requests unlocked.

    The rest is what was submitted, and the applicant cannot change it now:
    checked too, a gap only the call team can mend -- a compliance question
    added after submission, a required field left empty before it was
    required -- would refuse the changes and leave the request open for good.
    ``Proposal.can_submit_changes`` reports the same verdict to the form.
    """
    fields = {
        part[len(ChangeRequestParts.FIELD_PREFIX) :]
        for part in parts
        if part.startswith(ChangeRequestParts.FIELD_PREFIX)
    }
    if ChangeRequestParts.DOCUMENTS in parts:
        fields.add("supporting_documentation")
    if fields:
        validate_project_details_complete(proposal, fields)
    if ChangeRequestParts.RESOURCES in parts:
        validate_resource_templates_used(proposal)
        validate_requested_amounts_present(proposal)
        validate_required_options_present(proposal)
        validate_requests_within_budgets(proposal)
        validate_resources_requested_for_offering_steps(proposal)
        validate_purchase_orders_present(proposal)
    if ChangeRequestParts.TEAM in parts:
        validate_proposal_manager_present(proposal)
    if ChangeRequestParts.DURATION_FIELD in fields:
        validate_requested_duration(proposal)
    if ChangeRequestParts.COMPLIANCE in parts:
        validate_compliance_answered(proposal)


def reviewers_with_email_invitation(call, reviewers):
    """IDs of the reviewers already invited to the call by email.

    An email invitation has no reviewer until it is accepted, so the
    (call, reviewer) lookup misses it. Inviting the same person again would
    leave two rows, and accepting the email one would then hit
    unique_call_reviewer.
    """
    email_invitations = list(
        models.CallReviewerPool.objects.filter(
            call=call, reviewer__isnull=True
        ).values_list("invited_user_id", "invited_email")
    )
    if not email_invitations:
        return set()
    invited_user_ids = {user_id for user_id, _ in email_invitations if user_id}
    invited_emails = {email.lower() for _, email in email_invitations if email}
    return {
        reviewer.id
        for reviewer in reviewers
        if reviewer.user_id in invited_user_ids
        or (reviewer.user.email and reviewer.user.email.lower() in invited_emails)
    }


def _may_read_call_workflow(user, call) -> bool:
    """Whether ``user`` reads the call's workflow configuration: its
    description, checks, versions and changes. The call's team -- those who
    configure it -- staff and support; never the panel, reviewers or
    applicants."""
    return bool(
        user.is_staff
        or user.is_support
        or proposal_permissions.configurable_call_ids(user).filter(pk=call.pk).exists()
    )


# Who _may_read_call_workflow lets read a call's workflow configuration, on a
# call the caller sees. Change the two together.
_CALL_WORKFLOW_READERS = (
    "those who configure the call (CALL.UPDATE on the call, on its call "
    "managing organisation or on the organization running it) and users with "
    "the staff or support flag, read from the user whatever their token's "
    "scopes; anyone else who sees the call gets 404."
)
CALL_WORKFLOW_READ_ACCESS = access.custom("For " + _CALL_WORKFLOW_READERS)
# The same, on a route whose writes are checked as the call's other writes.
CALL_WORKFLOW_READ_WRITE_ACCESS = access.custom(
    "A read is for "
    + _CALL_WORKFLOW_READERS
    + " A write needs CALL.UPDATE, as the call's other writes do."
)


def _workflow_check_response(findings):
    return response.Response(
        serializers.WorkflowCheckSerializer(
            {
                "findings": findings,
                "summary": workflow_checks.summary(findings),
                "can_activate": not workflow_checks.errors(findings),
            }
        ).data
    )


_WORKFLOW_VERSION_NUMBER = OpenApiParameter(
    "number",
    OpenApiTypes.INT,
    OpenApiParameter.PATH,
    description="Number of the version.",
)
_WORKFLOW_DIFF_PARAMETERS = [
    OpenApiParameter(
        "from",
        OpenApiTypes.STR,
        OpenApiParameter.QUERY,
        description=(
            "Version to compare from: a version number, 'current', and on a "
            "call 'template' (the template version it was created from, the "
            "default) or 'template_latest' (its template as it is now)."
        ),
    ),
    OpenApiParameter(
        "to",
        OpenApiTypes.STR,
        OpenApiParameter.QUERY,
        description="Version to compare to; 'current' by default.",
    ),
]


def _template_info(call):
    template = call.template
    if template is None:
        return None
    return {
        "uuid": template.uuid,
        "name": template.name,
        "version": call.template_version,
        "latest_version": template.version,
    }


def _workflow_diff_response(owner, request, default_from):
    old_ref = request.query_params.get("from") or default_from
    new_ref = request.query_params.get("to") or "current"
    if isinstance(owner, models.Call) and "template_latest" in (old_ref, new_ref):
        # The template as it is now: for those who may read it, as the
        # template list shows it to them.
        template = owner.template
        if template is not None and not (
            request.user.is_staff or template.is_published
        ):
            raise exceptions.ValidationError(
                {"from": _("The template is no longer published.")}
            )
    old = workflow_versions.resolve(owner, old_ref)
    new = workflow_versions.resolve(owner, new_ref)
    if old is None or new is None:
        raise exceptions.ValidationError(
            {"from" if old is None else "to": _("No such version.")}
        )
    return response.Response(
        serializers.WorkflowDiffSerializer(
            {
                "from_version": old_ref,
                "to_version": new_ref,
                "template": _template_info(owner)
                if isinstance(owner, models.Call)
                else None,
                "changes": workflow_diff.diff(old, new),
            }
        ).data
    )


class CallTemplateViewSet(ActionsViewSet):
    """Call templates: call configurations a new call starts from.

    Staff author them. Anyone who may create a call in some call managing
    organisation lists and retrieves the published ones, without their
    document, and creates a call from one through import_call.
    """

    queryset = models.CallTemplate.objects.all().select_related("created_by")
    serializer_class = serializers.CallTemplateSerializer
    lookup_field = "uuid"
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.CallTemplateFilter

    create_permissions = [structure_permissions.is_staff]
    update_permissions = [structure_permissions.is_staff]
    partial_update_permissions = [structure_permissions.is_staff]
    destroy_permissions = [structure_permissions.is_staff]

    # Look the template up as retrieve does.
    access_filtered_actions = ("workflow_check", "workflow_description")

    @access.shows(
        "Users with the staff flag see every template, whatever their token's "
        "scopes; those holding CALL.CREATE on a call managing organisation see "
        "the published ones; anyone else sees none."
    )
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff:
            return queryset
        if proposal_permissions.may_create_calls(user):
            return queryset.filter(is_published=True)
        return queryset.none()

    def perform_create(self, serializer):
        template = call_templates.save_unique(
            lambda: serializer.save(created_by=self.request.user)
        )
        workflow_versions.record_on_commit(
            template, self.request.user, workflow_versions.Reasons.CREATED
        )

    def perform_update(self, serializer):
        template = call_templates.save_unique(serializer.save)
        workflow_versions.record_on_commit(template, self.request.user)

    @extend_schema(
        summary="List the call template presets",
        description=(
            "The call templates shipped with this portal: starting points for "
            "a template, with whether a template already holds the name each "
            "would be loaded under."
        ),
        responses={200: serializers.CallTemplatePresetSerializer(many=True)},
    )
    @decorators.action(
        detail=False, methods=["get"], filter_backends=[], pagination_class=None
    )
    def presets(self, request):
        return response.Response(
            serializers.CallTemplatePresetSerializer(
                call_templates.presets(), many=True
            ).data
        )

    presets_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Load call template presets",
        description=(
            "Create a call template from each preset given: a copy that keeps "
            "no link to the preset. Refused, with nothing created, when a "
            "template already holds the name a preset would be loaded under. "
            "The templates are published only if publish is set."
        ),
        request=serializers.LoadCallTemplatePresetSerializer,
        responses={200: serializers.LoadCallTemplatePresetResponseSerializer},
    )
    @decorators.action(detail=False, methods=["post"])
    def load_preset(self, request):
        serializer = serializers.LoadCallTemplatePresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = call_templates.load_presets(
            serializer.validated_data["presets"],
            publish=serializer.validated_data["publish"],
        )
        return response.Response(
            serializers.LoadCallTemplatePresetResponseSerializer(
                {
                    "created": len(result["created"]),
                    "created_names": result["created"],
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    load_preset_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Describe the template's workflow",
        description=(
            "What each step of the template's workflow does and who takes part "
            "in it: roles, applicant labels, notification rules and their "
            "flags, and the planned schedule. Rules only, never people."
        ),
        request=None,
        responses={200: serializers.WorkflowDescriptionSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-description")
    def workflow_description(self, request, uuid=None):
        template = self.get_object()
        return response.Response(
            serializers.WorkflowDescriptionSerializer(
                workflow_description.describe(template.document)
            ).data
        )

    @extend_schema(
        summary="Check the template's workflow",
        description=(
            "Errors, warnings and notes on the template's workflow, each with "
            "the setting that fixes it. Only the rules that read the document "
            "run: a template has no rounds, offerings or people."
        ),
        request=None,
        responses={200: serializers.WorkflowCheckSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-check")
    def workflow_check(self, request, uuid=None):
        template = self.get_object()
        return _workflow_check_response(workflow_checks.check(template.document))

    @extend_schema(
        summary="List the template's versions",
        description="Every version of the template's document, newest last.",
        request=None,
        responses={200: serializers.WorkflowVersionSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-versions")
    def workflow_versions(self, request, uuid=None):
        template = self.get_object()
        versions = workflow_versions.owner_versions(template).select_related(
            "created_by"
        )
        return response.Response(
            serializers.WorkflowVersionSerializer(versions, many=True).data
        )

    workflow_versions_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Get one version of the template",
        parameters=[_WORKFLOW_VERSION_NUMBER],
        request=None,
        responses={200: serializers.WorkflowVersionDetailSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        url_path=r"workflow-versions/(?P<number>[0-9]+)",
    )
    def workflow_version(self, request, uuid=None, number=None):
        template = self.get_object()
        version = get_object_or_404(
            workflow_versions.owner_versions(template), number=number
        )
        return response.Response(
            serializers.WorkflowVersionDetailSerializer(version).data
        )

    workflow_version_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Compare two versions of the template",
        parameters=_WORKFLOW_DIFF_PARAMETERS,
        request=None,
        responses={200: serializers.WorkflowDiffSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-diff")
    def workflow_diff(self, request, uuid=None):
        template = self.get_object()
        latest = workflow_versions.owner_versions(template).order_by("-number").first()
        default_from = (
            str(latest.number - 1) if latest and latest.number > 1 else "current"
        )
        return _workflow_diff_response(template, request, default_from)

    workflow_diff_permissions = [structure_permissions.is_staff]


def _template_reference(call):
    template = call.template
    if template is None:
        return None
    return {
        "uuid": template.uuid.hex,
        "name": template.name,
        "version": call.template_version,
    }


_REPORT_PARAMETERS = [
    OpenApiParameter(
        "round_uuid",
        OpenApiTypes.UUID,
        OpenApiParameter.QUERY,
        description="Report on one round of the calls. Not found when it "
        "is not a round of a call reported on.",
        extensions={"x-waldur-operation-id": "proposal_protected_calls_rounds_list"},
    ),
    OpenApiParameter(
        "submitted_from",
        OpenApiTypes.DATE,
        OpenApiParameter.QUERY,
        description="Count only proposals submitted on or after this day.",
    ),
    OpenApiParameter(
        "submitted_to",
        OpenApiTypes.DATE,
        OpenApiParameter.QUERY,
        description="Count only proposals submitted on or before this day.",
    ),
]

_REPORT_CALL_PARAMETER = OpenApiParameter(
    "call_uuid",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    many=True,
    description="Report on these calls of the organization only. Not found "
    "when one is not a call the user may report on.",
    extensions={"x-waldur-operation-id": "proposal_protected_calls_list"},
)

# What each report counts, by kind, for its OpenAPI description.
_REPORT_DESCRIPTIONS = {
    "portfolio": (
        "Per call and round: proposals submitted, the outcome mix, the "
        "success rate (accepted out of decided) and the mean overall score "
        "of the submitted reviews; with submissions per month."
    ),
    "workflow": (
        "Per workflow step, summed over the calls by step key: proposals "
        "waiting at it, overdue there, completed late, and the median and "
        "90th percentile days it took; with each call's own figures."
    ),
    "reviewers": (
        "Per reviewer of the proposals: assignments sent and how they were "
        "answered, reviews by state and overdue, the median days to answer "
        "and to review, and the mean score given. Deactivated reviewers are "
        "listed too."
    ),
    "rounds": (
        "Per round: the days from its cut-off to the evaluation, deciding, "
        "the results' publication and their adoption, and how late the "
        "results came against its allocation date. With submission dates, "
        "the rounds whose submission window overlaps them."
    ),
    "budgets": (
        "Per resource pool, and per component awarded or asked for without "
        "one: what it offers, what was requested, awarded and remains, and "
        "what proposals still waiting for their decision ask of it. The "
        "submission dates do not apply."
    ),
}

_REPORT_ORGANISATION_PARAMETER = OpenApiParameter(
    "organisation_uuid",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    many=True,
    description="Report on the calls of these call managing organizations "
    "only. Not found when one is not a call managing organization.",
    extensions={"x-waldur-operation-id": "call_managing_organisations_list"},
)

_REPORT_GLOBAL_CALL_PARAMETER = OpenApiParameter(
    "call_uuid",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    many=True,
    description="Report on these calls only. Not found when one is not a call "
    "of the organizations reported on.",
    extensions={"x-waldur-operation-id": "proposal_protected_calls_list"},
)

_GLOBAL_REPORT_AUDIENCE = (
    "Over every call of every call managing organization, for staff and support."
)
_ORGANISATION_REPORT_AUDIENCE = (
    "Over the organization's calls the user may change the configuration "
    "of: all of them for its call organizers, staff and support."
)
_CALL_REPORT_AUDIENCE = "Over the call alone, for its managers, staff and support."


def _report_schema(kind, organisation):
    """What but the responses the OpenAPI schema says of report ``kind``,
    organisation-wide or of one call."""
    audience = _ORGANISATION_REPORT_AUDIENCE if organisation else _CALL_REPORT_AUDIENCE
    parameters = list(_REPORT_PARAMETERS)
    if organisation:
        parameters.insert(0, _REPORT_CALL_PARAMETER)
    return {
        "description": f"{_REPORT_DESCRIPTIONS[kind]} {audience}",
        "request": None,
        "parameters": parameters,
        "filters": False,
    }


def _global_report_schema(kind):
    """What but the responses the OpenAPI schema says of report ``kind``
    over every call."""
    return {
        "operation_id": f"call_managing_organisations_reports_{kind}_global_retrieve",
        "description": f"{_REPORT_DESCRIPTIONS[kind]} {_GLOBAL_REPORT_AUDIENCE}",
        "request": None,
        "parameters": [
            _REPORT_ORGANISATION_PARAMETER,
            _REPORT_GLOBAL_CALL_PARAMETER,
            *_REPORT_PARAMETERS,
        ],
        "filters": False,
    }


def _report_filters(request):
    params = request.query_params
    data = {
        name: params[name]
        for name in ("round_uuid", "submitted_from", "submitted_to")
        if params.get(name)
    }
    for name in ("call_uuid", "organisation_uuid"):
        if params.getlist(name):
            data[name] = params.getlist(name)
    serializer = serializers.ReportFilterSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _organisation_report(view, request, build, serializer_class):
    """Report ``build`` over the organisation's calls the user may report on."""
    scope = reports.scope_for_organisation(
        request, view.get_object(), _report_filters(request)
    )
    return response.Response(
        serializer_class(build(scope, request.user)).data, status=status.HTTP_200_OK
    )


def _global_report(request, build, serializer_class):
    """Report ``build`` over every call, for staff and support."""
    scope = reports.scope_for_all(_report_filters(request))
    return response.Response(
        serializer_class(build(scope, request.user)).data, status=status.HTTP_200_OK
    )


def _call_report(view, request, build, serializer_class):
    """Report ``build`` over the call alone."""
    scope = reports.scope_for_call(view.get_object(), _report_filters(request))
    return response.Response(
        serializer_class(build(scope, request.user)).data, status=status.HTTP_200_OK
    )


_ORGANISATION_REPORT_PERMISSIONS = [
    proposal_permissions.support_can_read(
        proposal_permissions.can_read_organisation_reports
    )
]
_GLOBAL_REPORT_PERMISSIONS = [
    proposal_permissions.support_can_read(structure_permissions.is_staff)
]
_CALL_REPORT_PERMISSIONS = [
    proposal_permissions.support_can_read(
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    )
]


class CallManagingOrganisationViewSet(
    UserRoleMixin, PublicViewsetMixin, BaseMarketplaceView
):
    lookup_field = "uuid"
    queryset = models.CallManagingOrganisation.objects.all().order_by("customer__name")
    serializer_class = serializers.CallManagingOrganisationSerializer
    filterset_class = filters.CallManagingOrganisationFilter

    # A directory of who runs calls, opened as the marketplace catalog is; the
    # serializer checks the organization named in a create.
    access_declarations = {
        "list": [PUBLIC_CATALOG_ACCESS],
        "retrieve": [PUBLIC_CATALOG_ACCESS],
        "create": [marketplace_permissions.REGISTERS_SERVICE_PROVIDER],
    }

    def destroy(self, request, *args, **kwargs):
        instance: models.CallManagingOrganisation = self.get_object()
        try:
            self.perform_destroy(instance)
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        except ProtectedError:
            return response.Response(
                {
                    "detail": "Cannot delete this call manager as there are existing connected calls or user permissions."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        description=(
            "Return statistics for call managing organization, counted over "
            "the active calls the caller may report on, as the organization "
            "reports are: a call manager's figures cover their own calls."
        ),
        request=None,
        responses=serializers.CallManagingOrganisationStatSerializer,
    )
    @decorators.action(detail=True)
    def stats(self, request, uuid=None):
        instance: models.CallManagingOrganisation = self.get_object()
        now = timezone.now()
        one_week_from_now = now + timedelta(weeks=1)
        # Counted over the calls the organisation reports cover, so a call
        # manager's figures leave out the organisation's other calls.
        calls = reports.organisation_calls(request, instance).filter(
            state=CallStates.ACTIVE
        )

        open_calls = calls.count()
        active_rounds = (
            models.Round.objects.filter(round_not_ended_q(now=now))
            .filter(call__in=calls)
            .count()
        )
        accepted_proposals = models.Proposal.objects.filter(
            state=ProposalStates.ACCEPTED,
            round__call__in=calls,
        ).count()
        pending_proposals = models.Proposal.objects.filter(
            state__in=[
                ProposalStates.IN_REVIEW,
                ProposalStates.SUBMITTED,
            ],
            round__call__in=calls,
        ).count()
        pending_review = models.Review.objects.filter(
            state=models.Review.States.SUBMITTED,
            proposal__round__call__in=calls,
        ).count()

        rounds_closing_in_one_week = models.Round.objects.filter(
            cutoff_time__gte=now,
            cutoff_time__lte=one_week_from_now,
            call__in=calls,
        ).count()

        calls_closing_in_one_week = (
            calls.filter(
                round__cutoff_time__gte=now,
                round__cutoff_time__lte=one_week_from_now,
            )
            .distinct()
            .count()
        )

        offering_requests_pending = models.RequestedOffering.objects.filter(
            state=RequestedOfferingStates.REQUESTED,
            call__in=calls,
        ).count()

        return response.Response(
            {
                "open_calls": open_calls,
                "active_rounds": active_rounds,
                "accepted_proposals": accepted_proposals,
                "pending_proposals": pending_proposals,
                "pending_review": pending_review,
                "rounds_closing_in_one_week": rounds_closing_in_one_week,
                "calls_closing_in_one_week": calls_closing_in_one_week,
                "offering_requests_pending": offering_requests_pending,
            },
            status=status.HTTP_200_OK,
        )

    stats_permissions = _ORGANISATION_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.PortfolioReportSerializer},
        **_report_schema("portfolio", organisation=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/portfolio")
    def reports_portfolio(self, request, uuid=None):
        return _organisation_report(
            self, request, reports.portfolio, serializers.PortfolioReportSerializer
        )

    reports_portfolio_permissions = _ORGANISATION_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.WorkflowReportSerializer},
        **_report_schema("workflow", organisation=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/workflow")
    def reports_workflow(self, request, uuid=None):
        return _organisation_report(
            self, request, reports.workflow, serializers.WorkflowReportSerializer
        )

    reports_workflow_permissions = _ORGANISATION_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.ReviewerReportSerializer},
        **_report_schema("reviewers", organisation=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/reviewers")
    def reports_reviewers(self, request, uuid=None):
        return _organisation_report(
            self, request, reports.reviewers, serializers.ReviewerReportSerializer
        )

    reports_reviewers_permissions = _ORGANISATION_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.RoundReportSerializer},
        **_report_schema("rounds", organisation=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/rounds")
    def reports_rounds(self, request, uuid=None):
        return _organisation_report(
            self, request, reports.rounds, serializers.RoundReportSerializer
        )

    reports_rounds_permissions = _ORGANISATION_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.BudgetReportSerializer},
        **_report_schema("budgets", organisation=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/budgets")
    def reports_budgets(self, request, uuid=None):
        return _organisation_report(
            self, request, reports.budgets_usage, serializers.BudgetReportSerializer
        )

    reports_budgets_permissions = _ORGANISATION_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.PortfolioReportSerializer},
        **_global_report_schema("portfolio"),
    )
    @no_count_action
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path="reports/portfolio",
        url_name="reports-portfolio-global",
    )
    def reports_portfolio_global(self, request):
        return _global_report(
            request, reports.portfolio, serializers.PortfolioReportSerializer
        )

    reports_portfolio_global_permissions = _GLOBAL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.WorkflowReportSerializer},
        **_global_report_schema("workflow"),
    )
    @no_count_action
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path="reports/workflow",
        url_name="reports-workflow-global",
    )
    def reports_workflow_global(self, request):
        return _global_report(
            request, reports.workflow, serializers.WorkflowReportSerializer
        )

    reports_workflow_global_permissions = _GLOBAL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.ReviewerReportSerializer},
        **_global_report_schema("reviewers"),
    )
    @no_count_action
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path="reports/reviewers",
        url_name="reports-reviewers-global",
    )
    def reports_reviewers_global(self, request):
        return _global_report(
            request, reports.reviewers, serializers.ReviewerReportSerializer
        )

    reports_reviewers_global_permissions = _GLOBAL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.RoundReportSerializer},
        **_global_report_schema("rounds"),
    )
    @no_count_action
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path="reports/rounds",
        url_name="reports-rounds-global",
    )
    def reports_rounds_global(self, request):
        return _global_report(
            request, reports.rounds, serializers.RoundReportSerializer
        )

    reports_rounds_global_permissions = _GLOBAL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.BudgetReportSerializer},
        **_global_report_schema("budgets"),
    )
    @no_count_action
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path="reports/budgets",
        url_name="reports-budgets-global",
    )
    def reports_budgets_global(self, request):
        return _global_report(
            request, reports.budgets_usage, serializers.BudgetReportSerializer
        )

    reports_budgets_global_permissions = _GLOBAL_REPORT_PERMISSIONS


class PublicCallViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "uuid"
    queryset = (
        models.Call.objects.filter(state__in=[CallStates.ACTIVE, CallStates.ARCHIVED])
        # Each template serializes its offering's components so the applicant
        # can be shown a price; without this that is a query per template.
        .prefetch_related(
            "resource_templates__requested_offering__offering__components",
            "resource_templates__requested_offering__plan__components",
        )
        .order_by("created")
    )
    serializer_class = serializers.PublicCallSerializer
    filterset_class = filters.PublicCallFilter
    permission_classes = (rf_permissions.AllowAny,)

    access_declarations = {
        "check_eligibility": [
            access.owned(
                "The caller's own eligibility to submit to an active or archived call."
            )
        ],
    }

    @extend_schema(
        description="Check if the current user is eligible to submit proposals to this call.",
        request=None,
        responses={200: serializers.EligibilityCheckSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        permission_classes=[rf_permissions.IsAuthenticated],
    )
    def check_eligibility(self, request, uuid=None):
        """Check if the current user is eligible to submit proposals to this call."""
        call = self.get_object()
        user = request.user

        restrictions = []
        for check in (
            permissions_utils.validate_user_restrictions,
            applicant_capacity.validate_applicant_capacity,
        ):
            try:
                check(call, user)
            except exceptions.ValidationError as e:
                restrictions.extend(_restriction_messages(e))
        data = {"is_eligible": not restrictions, "restrictions": restrictions}

        serializer = serializers.EligibilityCheckSerializer(data)
        return response.Response(serializer.data)


def _restriction_messages(error):
    """The messages of an eligibility ``ValidationError``, flattened."""
    detail = error.detail
    if isinstance(detail, list):
        return [str(msg) for msg in detail]
    if isinstance(detail, dict):
        messages = []
        for value in detail.values():
            if isinstance(value, list):
                messages.extend(str(v) for v in value)
            else:
                messages.append(str(value))
        return messages
    return [str(detail)]


# Shared by both exports. The extension names the endpoint the value comes
# from, as the schema validation requires of every UUID query parameter.
def _export_uuid_parameter(name: str, operation_id: str, description: str):
    return OpenApiParameter(
        name,
        OpenApiTypes.UUID,
        OpenApiParameter.QUERY,
        description=description,
        extensions={"x-waldur-operation-id": operation_id},
    )


_EXPORT_APPLICANT_PARAMETER = _export_uuid_parameter(
    "created_by_uuid", "users_list", "Limit the export to one applicant."
)
_EXPORT_REVIEWER_PARAMETER = _export_uuid_parameter(
    "reviewer_uuid", "users_list", "Limit the export to one reviewer."
)
_EXPORT_PROPOSAL_PARAMETER = OpenApiParameter(
    "proposal_uuid",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    many=True,
    description=(
        "Limit the export to these proposals, such as those selected in the "
        "list; repeat the parameter for each."
    ),
    extensions={"x-waldur-operation-id": "proposal_proposals_list"},
)

_EXPORT_NAME_PARAMETER = OpenApiParameter(
    "proposal_name",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    description=(
        "Limit the export to proposals whose name contains this text — the "
        "list's search box. Named proposal_name because a bare `name` is read "
        "by the call's own filterset."
    ),
)

_EXPORT_ROUND_PARAMETER = OpenApiParameter(
    "round_uuid",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    description="Limit the export to one round.",
    extensions={"x-waldur-operation-id": "proposal_protected_calls_rounds_list"},
)


def _export_uuid(request, parameter: str) -> str | None:
    """A UUID filter, rejected early when it is not a UUID.

    Feeding a malformed value straight to the queryset raises a ValidationError
    from deep inside Django and comes back as a 500.
    """
    raw = request.query_params.get(parameter)
    if not raw:
        return None
    try:
        uuid_module.UUID(raw)
    except (TypeError, ValueError):
        raise exceptions.ValidationError({parameter: _("Not a valid UUID.")}) from None
    return raw


def _export_uuids(request, parameter: str) -> list[str]:
    """A UUID filter that may repeat, each value checked as ``_export_uuid``
    checks one."""
    values = request.query_params.getlist(parameter)
    for raw in values:
        try:
            uuid_module.UUID(raw)
        except (TypeError, ValueError):
            raise exceptions.ValidationError(
                {parameter: _("Not a valid UUID.")}
            ) from None
    return values


def _export_states(request, parameter: str, choices) -> list[str]:
    """The state filter, restricted to the states the model defines.

    Named ``proposal_state`` / ``review_state`` rather than ``state``: the
    permission check resolves the call through ``get_object()``, which still
    runs the call's own list filterset, and a bare ``state`` there is read as a
    *call* state and rejected.

    An unknown state is refused rather than ignored: silently returning every
    row would hand the user a file they believe is filtered.
    """
    states = request.query_params.getlist(parameter)
    valid = {state for state, _label in choices}
    unknown = [state for state in states if state not in valid]
    if unknown:
        raise exceptions.ValidationError(
            {parameter: _("Unknown state: %s.") % ", ".join(sorted(unknown))}
        )
    return states


class WorkflowDryRunThrottle(UserRateThrottle):
    """Dry runs per user: each replays a whole path in a transaction."""

    scope = "workflow_dry_run"


class WorkflowReportThrottle(UserRateThrottle):
    """Workflow reports per user: each describes and checks the whole call
    and renders a PDF."""

    scope = "workflow_report"


class CallImportThrottle(UserRateThrottle):
    """Call imports per user: each creates a call and its whole workflow in
    one transaction."""

    scope = "call_import"


class ProtectedCallViewSet(UserRoleMixin, ActionsViewSet, ActionMethodMixin):
    lookup_field = "uuid"
    serializer_class = serializers.ProtectedCallSerializer
    filterset_class = filters.CallFilter
    filter_backends = [DjangoFilterBackend]
    destroy_validators = [core_validators.StateValidator(CallStates.DRAFT)]
    # An archived call is read-only: block core field edits (name/description/
    # reference_code/external_url/fixed_duration + visibility + COI settings)
    # while keeping draft and active calls fully editable.
    update_validators = partial_update_validators = [
        core_validators.StateValidator(CallStates.DRAFT, CallStates.ACTIVE)
    ]
    # Every write to a call -- core fields, state transitions, rounds, offerings,
    # documents, workflow steps -- requires UPDATE_CALL. "*" covers the call
    # manager's role on the call itself; "manager" reaches the
    # CallManagingOrganisation where an organizer's CUSTOMER.CALL_ORGANIZER role
    # is bound. Actions declaring their own <action>_permissions override this.
    unsafe_methods_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]
    # POST to the list route has no object to check against; creation is gated
    # by CREATE_CALL on the managing organisation in the serializer.
    create_permissions = []
    # Team management carries its own permission set -- CALL.CREATE_PERMISSION /
    # UPDATE_PERMISSION / DELETE_PERMISSION, enforced against the call or its
    # customer in UserRoleMutateSerializer.validate. It is deliberately separate
    # from UPDATE_CALL: the shipped CUSTOMER.OWNER role holds the former and not
    # the latter, so letting the blanket gate cover these inherited UserRoleMixin
    # actions would take call-team management away from owners.
    add_user_permissions = []
    update_user_permissions = []
    delete_user_permissions = []

    queryset = models.Call.objects.all().select_related("template")

    # Reads of the call's own rounds, offerings, templates, component rules,
    # evaluator roles and workflow steps: each looks the call up as retrieve
    # does. Who reads a workflow step's team fields is narrowed further.
    access_filtered_actions = (
        "offerings",
        "offering_detail",
        "rounds",
        "round_detail",
        "resource_templates",
        "resource_template_detail",
        "offering_components",
        "offering_component_detail",
        "workflow_steps",
        "workflow_step_detail",
        "proposal_roles",
        "proposal_role_detail",
    )

    access_declarations = {
        # Checked by ProtectedCallSerializer: validate_manager and create().
        "create": [
            access.custom(
                "CALL.CREATE on the call managing organisation named in "
                "manager and, unless the user has the staff flag, "
                "CALL.CREATE_PERMISSION on it too."
            )
        ],
        # Checked by CallImportParametersSerializer.validate_manager.
        "import_call": [
            access.custom(
                "CALL.CREATE on the call managing organisation named in "
                "manager. Only users with the staff flag may name an "
                "unpublished template."
            )
        ],
        # Checked in the action body: _workflow_reader_call.
        "workflow_description": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_report": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_progress": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_paths": [CALL_WORKFLOW_READ_ACCESS],
        "scenario_results": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_check": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_versions": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_version": [CALL_WORKFLOW_READ_ACCESS],
        "workflow_diff": [CALL_WORKFLOW_READ_ACCESS],
        "notification_mail_types": [CALL_WORKFLOW_READ_ACCESS],
        "notification_preview": [CALL_WORKFLOW_READ_ACCESS],
        "notification_overrides": [CALL_WORKFLOW_READ_WRITE_ACCESS],
        "notification_override_detail": [CALL_WORKFLOW_READ_WRITE_ACCESS],
        # Checked in the action body: may_configure_calls.
        "step_checklists": [
            access.custom(
                "Those who configure a call or may create one (CALL.UPDATE on "
                "a call, its call managing organisation or organization, or "
                "CALL.CREATE on a call managing organisation or organization) "
                "and users with the staff or support flag, whatever their "
                "token's scopes. Each sees the shared forms and those of the "
                "organizations whose calls they configure or may create; "
                "staff and support see every form."
            )
        ],
        # The kinds are code, the same for every call.
        "workflow_step_kinds": [access.authenticated()],
        "dashboard_stats": [
            access.owned(
                "Counts over the calls where the caller holds the call "
                "manager role on the call itself; zeros without one."
            )
        ],
    }

    @access.shows(
        "Users with the staff or support flag see every call, whatever their "
        "token's scopes, as does a site-wide CALL.LIST grant. Anyone else sees "
        "the calls of organizations where they hold CALL.LIST, those of call "
        "managing organisations where they hold a role, and the calls they "
        "hold a role on, such as call manager or reviewer."
    )
    def get_queryset(self):
        return filter_queryset_for_user(
            super().get_queryset(), self.request.user
        ).order_by("created")

    # Writes that change what the call exports as its settings and workflow
    # record a workflow version once they commit. Every other write action is
    # listed in VERSION_EXEMPT_ACTIONS, or records its version itself.
    VERSIONED_ACTIONS = frozenset(
        {
            "update",
            "partial_update",
            "workflow_steps",
            "workflow_step_detail",
            "proposal_roles",
            "proposal_role_detail",
            "coi_configuration",
            "matching_configuration",
            # The reviewers' response window, as the version diff shows it.
            "assignment_configuration",
            # The domain panels travel with the call's settings.
            "panels",
            "panel_detail",
            # So does the call's own wording of its mail.
            "notification_overrides",
            "notification_override_detail",
        }
    )
    VERSION_SELF_RECORDING_ACTIONS = frozenset(
        {"create", "activate", "import_call", "duplicate", "save_as_template"}
    )
    VERSION_EXEMPT_ACTIONS = frozenset(
        {
            # The call goes, or only its state or people change.
            "destroy",
            "archive",
            "add_user",
            "update_user",
            "delete_user",
            # Rounds, offerings, documents and resource templates are one
            # call's own, as they are left out of a template.
            "rounds",
            "rounds_bulk_set",
            "round_detail",
            "close_round",
            "start_deciding_round",
            "publish_results_round",
            "complete_round",
            "record_adoption_round",
            "complete_workflow_step_round",
            "open_workflow_step_round",
            "advance_workflow_step_round",
            "set_workflow_step_due_date_round",
            "allocate_in_submission_order_round",
            "offerings",
            "offering_detail",
            "resource_templates",
            "resource_template_detail",
            # Component rules and resource pools are rows of the call's
            # offerings, left out with them.
            "offering_components",
            "offering_component_detail",
            "resource_pools",
            "resource_pool_detail",
            "attach_documents",
            "detach_documents",
            # Running the call: proposals, reviewers and assignments.
            "notify_proposals",
            "compute_affinities",
            "create_manual_assignment",
            "detect_conflicts",
            "generate_assignments",
            "generate_suggestions",
            "invite_by_email",
            "review_proposal_compliance",
            "reviewer_pool",
            "send_all_assignments",
            "send_invitations",
            # Reads, though posted; a dry run rolls back whatever it does.
            "export_call",
            "workflow_dry_run",
            "notification_preview_draft",
            # Dry runs too: only their results are kept, on the latest version.
            "run_workflow_scenarios",
        }
    )

    def perform_create(self, serializer):
        call = serializer.save()
        workflow_versions.record_on_commit(
            call, self.request.user, workflow_versions.Reasons.CREATED
        )

    def finalize_response(self, request, response, *args, **kwargs):
        if (
            request.method not in rf_permissions.SAFE_METHODS
            and self.action in self.VERSIONED_ACTIONS
            and 200 <= response.status_code < 300
            and self.kwargs.get("uuid")
        ):
            call = models.Call.objects.filter(uuid=self.kwargs["uuid"]).first()
            if call is not None:
                workflow_versions.record_on_commit(call, request.user)
        return super().finalize_response(request, response, *args, **kwargs)

    def can_view_scope_team(self, user, call):
        # Core walks the call's customer/project tree (customer_path =
        # "manager__customer"), which never reaches the CallManagingOrganisation
        # where a Call organizer's CUSTOMER.CALL_ORGANIZER role is actually bound.
        # Without this, an organiser opening the "Team" tab of a call they can
        # edit gets a 403 on list_users while staff/owners pass. The call creator
        # is covered explicitly for preset/imported calls where the role grant
        # never ran.
        if call.created_by_id == user.id:
            return True
        if super().can_view_scope_team(user, call):
            return True
        return call.manager_id in get_connected_call_organizers(user)

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_offerings_list",
        request=None,
        responses=serializers.RequestedOfferingSerializer(many=True),
        description="List offerings for a call.",
        parameters=[
            OpenApiParameter(
                "state", str, OpenApiParameter.QUERY, description="Filter by state"
            ),
        ],
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_offerings_set",
        request=serializers.RequestedOfferingSerializer,
        responses=serializers.RequestedOfferingSerializer,
        description="Create offering for a call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def offerings(self, request, uuid=None):
        if request.method == "GET":
            call = self.get_object()
            queryset = call.requestedoffering_set.all()

            # Apply state filter if provided - only filter with valid states
            state_filter = request.query_params.getlist("state")
            if state_filter:
                # Get valid state values from the enum
                valid_states = {choice[0] for choice in RequestedOfferingStates.CHOICES}
                # Filter to only include valid state values
                valid_state_filter = [s for s in state_filter if s in valid_states]
                if valid_state_filter:
                    queryset = queryset.filter(state__in=valid_state_filter)

            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(
                    page, context=self.get_serializer_context(), many=True
                )
                return self.get_paginated_response(serializer.data)
            serializer = self.get_serializer(
                queryset,
                context=self.get_serializer_context(),
                many=True,
            )
            return response.Response(serializer.data, status=status.HTTP_200_OK)

        return self.action_list_method(
            "requestedoffering_set",
            additional_validators=["validate_call_not_archived"],
        )(self, request, uuid)

    offerings_serializer_class = serializers.RequestedOfferingSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RequestedOfferingSerializer}
    )
    def offering_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "requestedoffering_set",
            delete_validators=[validate_call_not_archived, validate_no_dependents],
            update_validators=[
                validate_call_not_archived,
                core_validators.StateValidator(
                    models.RequestedOffering.States.REQUESTED
                ),
            ],
        )(self, request, uuid, obj_uuid)

    offering_detail_serializer_class = serializers.RequestedOfferingSerializer

    @extend_schema(
        description=(
            "Activate a call. Refused with every error the workflow check "
            "finds; warnings do not block."
        ),
        request=None,
        responses={
            200: serializers.MessageResponseSerializer,
            400: serializers.CallActivationRefusedSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        call: models.Call = self.get_object()
        # Every error at once: the call cannot run as configured. Warnings
        # never block.
        errors = workflow_checks.errors(workflow_checks.check_call(call))
        if errors:
            return response.Response(
                serializers.CallActivationRefusedSerializer(
                    {
                        "detail": " ".join(e["message"] for e in errors),
                        "workflow_check": errors,
                    }
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        call.state = CallStates.ACTIVE
        call.save()
        workflow_versions.record_on_commit(
            call, request.user, workflow_versions.Reasons.ACTIVATED
        )
        return response.Response(
            {"message": "Call has been activated."},
            status=status.HTTP_200_OK,
        )

    activate_validators = [
        core_validators.StateValidator(CallStates.DRAFT, CallStates.ARCHIVED)
    ]

    @extend_schema(
        description=(
            "Archive a call. A call with proposals still in evaluation is "
            "archived only with confirm: those proposals stop where they are."
        ),
        request=serializers.ArchiveCallSerializer,
        responses={200: serializers.MessageResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        call: models.Call = self.get_object()
        serializer = serializers.ArchiveCallSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        in_progress = models.Proposal.objects.filter(
            round__call=call,
            state__in=(ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW),
        ).count()
        if in_progress and not serializer.validated_data["confirm"]:
            return response.Response(
                {
                    "detail": (
                        f"{in_progress} proposals of the call are still in "
                        "evaluation and would stop where they are. Confirm to "
                        "archive the call anyway."
                    ),
                    "proposals_in_evaluation": in_progress,
                },
                status=status.HTTP_409_CONFLICT,
            )
        call.state = CallStates.ARCHIVED
        call.save()
        return response.Response(
            {"message": "Call has been archived."},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Proposal, review and workflow step counts per call and round.",
        request=None,
        responses={200: serializers.CallStatsSerializer},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def stats(self, request, uuid=None):
        return response.Response(_call_stats(self.get_object()))

    stats_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
        )
    ]

    @extend_schema(
        responses={200: serializers.PortfolioReportSerializer},
        **_report_schema("portfolio", organisation=False),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/portfolio")
    def reports_portfolio(self, request, uuid=None):
        return _call_report(
            self, request, reports.portfolio, serializers.PortfolioReportSerializer
        )

    reports_portfolio_permissions = _CALL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.WorkflowReportSerializer},
        **_report_schema("workflow", organisation=False),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/workflow")
    def reports_workflow(self, request, uuid=None):
        return _call_report(
            self, request, reports.workflow, serializers.WorkflowReportSerializer
        )

    reports_workflow_permissions = _CALL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.ReviewerReportSerializer},
        **_report_schema("reviewers", organisation=False),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/reviewers")
    def reports_reviewers(self, request, uuid=None):
        return _call_report(
            self, request, reports.reviewers, serializers.ReviewerReportSerializer
        )

    reports_reviewers_permissions = _CALL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.RoundReportSerializer},
        **_report_schema("rounds", organisation=False),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/rounds")
    def reports_rounds(self, request, uuid=None):
        return _call_report(
            self, request, reports.rounds, serializers.RoundReportSerializer
        )

    reports_rounds_permissions = _CALL_REPORT_PERMISSIONS

    @extend_schema(
        responses={200: serializers.BudgetReportSerializer},
        **_report_schema("budgets", organisation=False),
    )
    @decorators.action(detail=True, methods=["get"], url_path="reports/budgets")
    def reports_budgets(self, request, uuid=None):
        return _call_report(
            self, request, reports.budgets_usage, serializers.BudgetReportSerializer
        )

    reports_budgets_permissions = _CALL_REPORT_PERMISSIONS

    @extend_schema(
        operation_id="proposal_protected_calls_export_proposals",
        description=(
            "Download the call's proposals as CSV, one row per proposal, with "
            "one column per requested offering component. The column set is "
            "derived from the call, so it is the same for every row and "
            "between exports of the same call."
        ),
        parameters=[
            _EXPORT_ROUND_PARAMETER,
            _EXPORT_APPLICANT_PARAMETER,
            _EXPORT_PROPOSAL_PARAMETER,
            _EXPORT_NAME_PARAMETER,
            OpenApiParameter(
                "proposal_state",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                many=True,
                enum=[state for state, _label in ProposalStates.CHOICES],
                description="Limit the export to proposals in these states.",
            ),
        ],
        responses={(200, "text/csv"): OpenApiTypes.BINARY},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"], url_path="export-proposals")
    def export_proposals(self, request, uuid=None):
        call: models.Call = self.get_object()
        queryset = exports.proposal_queryset(
            call,
            round_uuid=_export_uuid(request, "round_uuid"),
            states=_export_states(request, "proposal_state", ProposalStates.CHOICES),
            applicant_uuid=_export_uuid(request, "created_by_uuid"),
            proposal_uuids=_export_uuids(request, "proposal_uuid"),
            name=request.query_params.get("proposal_name"),
        )
        schema = exports.CallExportSchema(call)
        return exports.csv_response(
            exports.proposal_rows(schema, queryset),
            f"{call.slug}-proposals.csv",
        )

    export_proposals_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
        )
    ]

    @extend_schema(
        operation_id="proposal_protected_calls_export_reviews",
        description=(
            "Download the call's reviews as CSV, one row per review. The "
            "reviewer's private comment is never included: the export is open "
            "to call managers, and the review API keeps that field from them."
        ),
        parameters=[
            _EXPORT_ROUND_PARAMETER,
            _EXPORT_REVIEWER_PARAMETER,
            _EXPORT_PROPOSAL_PARAMETER,
            _EXPORT_NAME_PARAMETER,
            OpenApiParameter(
                "review_state",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                many=True,
                enum=[state for state, _label in ReviewStates.CHOICES],
                description="Limit the export to reviews in these states.",
            ),
        ],
        responses={(200, "text/csv"): OpenApiTypes.BINARY},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"], url_path="export-reviews")
    def export_reviews(self, request, uuid=None):
        call: models.Call = self.get_object()
        queryset = exports.review_queryset(
            call,
            round_uuid=_export_uuid(request, "round_uuid"),
            states=_export_states(request, "review_state", ReviewStates.CHOICES),
            reviewer_uuid=_export_uuid(request, "reviewer_uuid"),
            proposal_uuids=_export_uuids(request, "proposal_uuid"),
            proposal_name=request.query_params.get("proposal_name"),
        )
        return exports.csv_response(
            exports.review_rows(exports.criterion_columns(call), queryset),
            f"{call.slug}-reviews.csv",
        )

    export_reviews_permissions = export_proposals_permissions

    @extend_schema(
        operation_id="proposal_protected_calls_duplicate",
        description=(
            "Duplicate a call. The new call inherits the source call's "
            "configuration (offerings, rounds, workflow steps, resource "
            "templates, role mappings, documents, and COI/matching/"
            "assignment/applicant-visibility settings) and starts in draft "
            "state. Proposals, reviews, team permissions, and reviewer-pool "
            "memberships are not copied."
        ),
        request=serializers.DuplicateCallRequestSerializer,
        responses={201: serializers.ProtectedCallSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def duplicate(self, request, uuid=None):
        source = self.get_object()
        payload = serializers.DuplicateCallRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        new_name = payload.validated_data.pop("name")
        new_call = utils.duplicate_call(
            source=source,
            new_name=new_name,
            created_by=request.user,
            sections=payload.validated_data,
        )
        workflow_versions.record_on_commit(
            new_call, request.user, workflow_versions.Reasons.DUPLICATED
        )
        return response.Response(
            serializers.ProtectedCallSerializer(
                new_call, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    duplicate_permissions = [
        permission_factory(PermissionEnum.CREATE_CALL, ["manager"])
    ]
    duplicate_serializer_class = serializers.DuplicateCallRequestSerializer

    @extend_schema(
        summary="Export call configuration",
        description=(
            "Export the call's configuration as a portable document that "
            "import_call can recreate on another portal. Offerings, plans, "
            "checklists and roles are referenced by name. Proposals, reviews, "
            "reviewer pools, assignments and user references are never "
            "exported."
        ),
        request=serializers.CallExportParametersSerializer,
        responses=serializers.CallExportResponseSerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def export_call(self, request, uuid=None):
        call = self.get_object()
        params = serializers.CallExportParametersSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        sections = params.sections()
        export_data, warnings = call_transfer.export_call(call, sections)
        return response.Response(
            serializers.CallExportResponseSerializer(
                {
                    "call_uuid": call.uuid,
                    "call_name": call.name,
                    "export_data": export_data,
                    "exported_sections": [s for s, on in sections.items() if on],
                    "export_timestamp": timezone.now(),
                    "warnings": warnings,
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    export_call_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]
    export_call_serializer_class = serializers.CallExportParametersSerializer

    @extend_schema(
        summary="Describe the call's workflow",
        description=(
            "What each step of the call's workflow does and who takes part in "
            "it: roles, applicant labels, notification rules and their flags, "
            "and the planned schedule. Rules and counts only, never people. "
            "For those who configure the call, staff and support."
        ),
        request=None,
        responses={200: serializers.WorkflowDescriptionSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-description")
    def workflow_description(self, request, uuid=None):
        call = self._workflow_reader_call()
        return response.Response(
            serializers.WorkflowDescriptionSerializer(
                workflow_description.describe_call(call)
            ).data
        )

    @extend_schema(
        summary="Download the call's workflow validation as PDF",
        description=(
            "The call's workflow validation on paper: the workflow check's "
            "findings, the steps, who sees, contributes to, completes and is "
            "told about each, and the last scenario run. "
            "Rules and counts only, never people. For those who configure the "
            "call, staff and support."
        ),
        request=None,
        responses={(200, "application/pdf"): OpenApiTypes.BINARY},
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        url_path="workflow-report",
        throttle_classes=[WorkflowReportThrottle],
    )
    def workflow_report(self, request, uuid=None):
        call = self._workflow_reader_call()
        content = workflow_report.create_workflow_report_pdf(call)
        result = HttpResponse(content, content_type="application/pdf")
        result["Content-Disposition"] = (
            f'attachment; filename="{call.slug or call.uuid.hex}-workflow.pdf"'
        )
        return result

    def _requested_round(self, call):
        """The round named by the ``round`` query parameter, or None."""
        value = self.request.query_params.get("round")
        if not value:
            return None
        try:
            return call.round_set.get(uuid=value)
        except (models.Round.DoesNotExist, ValueError, DjangoValidationError):
            raise exceptions.ValidationError({"round": "No such round in this call."})

    @extend_schema(
        summary="Workflow progress of the call's proposals",
        description=(
            "Per step: proposals waiting at it now and how many of them are "
            "overdue, median and 90th-percentile days completed instances "
            "took against the planned duration, skipped, expired and "
            "withdrawn counts, and the share of each outcome. Counts only. "
            "For those who configure the call, staff and support."
        ),
        parameters=[
            OpenApiParameter(
                "round",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Count only this round's proposals.",
                extensions={
                    "x-waldur-operation-id": "proposal_protected_calls_rounds_list"
                },
            )
        ],
        request=None,
        responses={200: serializers.WorkflowProgressSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-progress")
    def workflow_progress(self, request, uuid=None):
        call = self._workflow_reader_call()
        call_round = self._requested_round(call)
        return response.Response(
            serializers.WorkflowProgressSerializer(
                workflow_observability.progress(call, call_round, user=request.user)
            ).data
        )

    @extend_schema(
        summary="Paths the call's proposals took through its workflow",
        description=(
            "Each distinct sequence of steps and outcomes the proposals took, "
            "with how many took it and whether a saved scenario covers it. "
            "For those who configure the call, staff and support."
        ),
        parameters=[
            OpenApiParameter(
                "round",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Only this round's proposals.",
                extensions={
                    "x-waldur-operation-id": "proposal_protected_calls_rounds_list"
                },
            )
        ],
        request=None,
        responses={200: serializers.WorkflowPathsSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-paths")
    def workflow_paths(self, request, uuid=None):
        call = self._workflow_reader_call()
        call_round = self._requested_round(call)
        return response.Response(
            serializers.WorkflowPathsSerializer(
                workflow_observability.paths(call, call_round, user=request.user)
            ).data
        )

    def _workflow_reader_call(self):
        call = self.get_object()
        # The call's assignment and visibility rules are its team's
        # configuration: the panel, reviewers and applicants do not read them.
        if not _may_read_call_workflow(self.request.user, call):
            raise Http404
        return call

    def _scenarios_response(self, call, status_code=status.HTTP_200_OK):
        scenarios = workflow_scenarios.call_scenarios(call)
        version, checks = workflow_scenarios.stored_scenario_checks(call)
        run_status, requested_at = workflow_scenarios.scenario_run_status(call)
        return response.Response(
            serializers.CallWorkflowScenariosSerializer(
                {
                    "status": run_status,
                    "requested_at": requested_at,
                    "error": workflow_scenarios.scenarios_error(call),
                    "template": _template_reference(call),
                    "scenarios": [
                        {
                            "key": s.get("key", ""),
                            "name": s.get("name", ""),
                            "description": s.get("description", ""),
                        }
                        for s in scenarios
                    ],
                    "version": version.number if version else None,
                    "checked_at": checks.get("checked_at"),
                    "results": checks.get("scenarios") or [],
                    "totals": (checks.get("totals") or {}).get("scenarios"),
                    "coverage": checks.get("coverage"),
                }
            ).data,
            status=status_code,
        )

    @extend_schema(
        summary="The call's saved scenarios and their last results",
        description=(
            "The scenarios of the template the call was created from, and "
            "how they went when last run on the call: passed, failed at the "
            "first assertion that did not hold, or an error. 'running' while "
            "a run asked for has not finished. For those who configure the "
            "call, staff and support."
        ),
        request=None,
        responses={200: serializers.CallWorkflowScenariosSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-scenarios")
    def scenario_results(self, request, uuid=None):
        call = self._workflow_reader_call()
        return self._scenarios_response(call)

    @extend_schema(
        summary="Run the call's saved scenarios",
        description=(
            "Dry-run each scenario of the template the call was created from "
            "on the call, at most 20, in the background, and keep the results "
            "on the call's latest workflow version; a run already on its way "
            "is not started again. Answers at once with the results so far "
            "and status 'running': poll workflow-scenarios until its status "
            "is 'idle' again. Each run is rolled back: nothing is kept or "
            "sent. Results are not kept when another dry run of the call got "
            "in the way; 'error' then says so. For those who configure the "
            "call, and staff."
        ),
        request=None,
        responses={202: serializers.CallWorkflowScenariosSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["post"],
        url_path="run-workflow-scenarios",
        throttle_classes=[WorkflowDryRunThrottle],
    )
    def run_workflow_scenarios(self, request, uuid=None):
        call = self.get_object()
        workflow_scenarios.request_call_scenarios(call, request.user)
        return self._scenarios_response(call, status.HTTP_202_ACCEPTED)

    run_workflow_scenarios_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]

    @extend_schema(
        summary="Check the call's workflow",
        description=(
            "Errors, warnings and notes on the call's workflow, each with the "
            "setting that fixes it. Errors stop the call's activation; "
            "warnings do not. For those who configure the call, staff and "
            "support."
        ),
        request=None,
        responses={200: serializers.WorkflowCheckSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-check")
    def workflow_check(self, request, uuid=None):
        call = self._workflow_reader_call()
        return _workflow_check_response(workflow_checks.check_call(call))

    @extend_schema(
        summary="List the call's workflow versions",
        description=(
            "Every recorded version of the call's settings and workflow, "
            "oldest first, with who made it and how many changes it holds. "
            "For those who configure the call, staff and support."
        ),
        request=None,
        responses={200: serializers.WorkflowVersionSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-versions")
    def workflow_versions(self, request, uuid=None):
        call = self._workflow_reader_call()
        workflow_versions.ensure_baseline(call)
        versions = workflow_versions.owner_versions(call).select_related("created_by")
        return response.Response(
            serializers.WorkflowVersionSerializer(versions, many=True).data
        )

    @extend_schema(
        summary="Get one workflow version of the call",
        parameters=[_WORKFLOW_VERSION_NUMBER],
        request=None,
        responses={200: serializers.WorkflowVersionDetailSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        url_path=r"workflow-versions/(?P<number>[0-9]+)",
    )
    def workflow_version(self, request, uuid=None, number=None):
        call = self._workflow_reader_call()
        version = get_object_or_404(
            workflow_versions.owner_versions(call), number=number
        )
        return response.Response(
            serializers.WorkflowVersionDetailSerializer(version).data
        )

    @extend_schema(
        summary="Compare two workflow versions of the call",
        description=(
            "The changes between two versions of the call's settings and "
            "workflow, by step and field, each marked as behavior or "
            "cosmetic. By default, from the template version the call was "
            "created from to the call as it is now."
        ),
        parameters=_WORKFLOW_DIFF_PARAMETERS,
        request=None,
        responses={200: serializers.WorkflowDiffSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-diff")
    def workflow_diff(self, request, uuid=None):
        call = self._workflow_reader_call()
        workflow_versions.ensure_baseline(call)
        if call.template_id is None:
            default_from = "1"
        else:
            default_from = "template"
        return _workflow_diff_response(call, request, default_from)

    @extend_schema(
        summary="Dry-run the call's workflow",
        description=(
            "Replay a path through the call's workflow on a synthetic "
            "proposal and return the state of every step, the log, every "
            "mail the path would send rendered with the real templates, and "
            "the outcomes tried. Stateless: each request replays its whole "
            "list of actions, and rewinding is a shorter list. Nothing is "
            "sent or kept: the run is rolled back. With view_as, the proposal "
            "as a role of the workflow description would read it, through "
            "the real read endpoints, as a synthetic holder of that role. "
            "For those who configure the call, and staff; one run per call "
            "at a time."
        ),
        request=serializers.WorkflowDryRunParametersSerializer,
        responses={200: serializers.WorkflowDryRunSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["post"],
        url_path="workflow-dry-run",
        throttle_classes=[WorkflowDryRunThrottle],
    )
    def workflow_dry_run(self, request, uuid=None):
        call = self.get_object()
        params = serializers.WorkflowDryRunParametersSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        view_as = data.get("view_as")
        if view_as and view_as["role"] not in dry_run.viewable_actors(
            workflow_description.describe_call(call)
        ):
            raise exceptions.ValidationError(
                {"view_as": {"role": "Not a role of the call's workflow."}}
            )
        # Only what the read endpoints need to build their links.
        origin = {
            name: request.META[name]
            for name in ("SERVER_NAME", "SERVER_PORT", "HTTP_HOST", "wsgi.url_scheme")
            if name in request.META
        }
        try:
            result = dry_run.run(call, data, origin=origin)
        except dry_run.DryRunError as e:
            return response.Response(
                {"detail": e.message, "action": e.action, "step": e.step},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except dry_run.DryRunBusy:
            return response.Response(
                {"detail": "Another dry run of this call is in progress; try again."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except dry_run.DryRunTimeout:
            return response.Response(
                {"detail": "The dry run took too long; try a shorter path."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            # A side effect reached, or a fault of the engine: logged in full
            # for the operators, told without detail to the caller.
            logger.exception("Dry run of the workflow of call %s failed", call.uuid)
            return response.Response(
                {"detail": "The dry run failed. Nothing was sent or saved."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        result["scenario_draft"] = workflow_scenarios.scenario_draft(
            call, json.loads(json.dumps(data, default=str)), result
        )
        return response.Response(serializers.WorkflowDryRunSerializer(result).data)

    workflow_dry_run_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]
    workflow_dry_run_serializer_class = serializers.WorkflowDryRunParametersSerializer

    # -- the call's own wording of its mail ------------------------------

    @extend_schema(
        summary="Mail types the call may word itself",
        description=(
            "Every mail type the call sends that its managers may word "
            "themselves: the variables its wording may use, the portal's "
            "wording it starts from, and the call's own wording if any. For "
            "those who configure the call, staff and support."
        ),
        request=None,
        responses={200: serializers.CallNotificationMailTypeSerializer(many=True)},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def notification_mail_types(self, request, uuid=None):
        call = self._workflow_reader_call()
        overrides = {
            (o.mail_type, o.variant): o
            for o in call.notification_overrides.select_related("updated_by")
        }
        rows = []
        for definition in notification_overrides.MAIL_TYPES:
            variant = notification_overrides.portal_template_variant(definition)
            rows.append(
                {
                    "key": definition.key,
                    "mail_type": definition.mail_type,
                    "variant": definition.variant,
                    "label": definition.label,
                    "audience": definition.audience,
                    "variables": [
                        {"name": name, "description": description}
                        for name, description, _ in definition.variables
                    ],
                    "portal_wording": notification_overrides.portal_sources(definition),
                    "portal_overridden": notification_overrides.portal_overridden(
                        definition.mail_type, variant
                    ),
                    "override": overrides.get(
                        (definition.mail_type, definition.variant)
                    ),
                }
            )
        return response.Response(
            serializers.CallNotificationMailTypeSerializer(rows, many=True).data
        )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_notification_overrides_list",
        request=None,
        responses=serializers.CallNotificationOverrideSerializer(many=True),
        description=(
            "The call's own wording of the mail it sends. For those who "
            "configure the call, staff and support."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_notification_overrides_set",
        request=serializers.CallNotificationOverrideSerializer,
        responses=serializers.CallNotificationOverrideSerializer,
        description=(
            "Word a mail of the call itself: subject, text body and HTML "
            "body, replacing the portal's wording for that mail on this "
            "call. Replaces the call's earlier wording of the same mail. "
            "Validated by rendering it in the sandbox against an example "
            "context; errors are given by field."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def notification_overrides(self, request, uuid=None):
        if request.method == "GET":
            call = self._workflow_reader_call()
            return response.Response(
                serializers.CallNotificationOverrideSerializer(
                    call.notification_overrides.select_related("updated_by"),
                    many=True,
                ).data
            )
        call = self.get_object()
        if call.state == CallStates.ARCHIVED:
            raise IncorrectStateException()
        serializer = serializers.CallNotificationOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            models.Call.objects.select_for_update().get(pk=call.pk)
            override, created = (
                models.CallNotificationOverride.objects.update_or_create(
                    call=call,
                    mail_type=data["mail_type"],
                    variant=data["variant"],
                    defaults={
                        "subject": data["subject"],
                        "body_text": data["body_text"],
                        "body_html": data.get("body_html", ""),
                        "enabled": data.get("enabled", True),
                        "updated_by": request.user,
                    },
                )
            )
        return response.Response(
            serializers.CallNotificationOverrideSerializer(override).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    notification_overrides_serializer_class = (
        serializers.CallNotificationOverrideSerializer
    )

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallNotificationOverrideSerializer}
    )
    def notification_override_detail(self, request, uuid=None, obj_uuid=None):
        if request.method == "GET":
            call = self._workflow_reader_call()
        else:
            call = self.get_object()
            if call.state == CallStates.ARCHIVED:
                raise IncorrectStateException()
        override = call.notification_overrides.filter(uuid=obj_uuid).first()
        if override is None:
            raise Http404
        if request.method == "DELETE":
            override.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        if request.method in ("PUT", "PATCH"):
            serializer = serializers.CallNotificationOverrideSerializer(
                override, data=request.data, partial=request.method == "PATCH"
            )
            serializer.is_valid(raise_exception=True)
            override = serializer.save(updated_by=request.user)
        return response.Response(
            serializers.CallNotificationOverrideSerializer(override).data
        )

    notification_override_detail_serializer_class = (
        serializers.CallNotificationOverrideSerializer
    )

    def _notification_preview_response(self, call, params, draft=None):
        try:
            result = notification_preview.preview(call, params, draft=draft)
        except notification_preview.PreviewError as error:
            return response.Response(error.errors, status=status.HTTP_400_BAD_REQUEST)
        except dry_run.DryRunBusy:
            return response.Response(
                {"detail": "A dry run of this call is in progress; try again."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except dry_run.DryRunTimeout:
            return response.Response(
                {"detail": "The preview took too long."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception("Preview of a mail of call %s failed", call.uuid)
            return response.Response(
                {"detail": "The preview failed. Nothing was sent or saved."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return response.Response(serializers.NotificationPreviewSerializer(result).data)

    @extend_schema(
        summary="Preview a mail of the call",
        description=(
            "The mail a notification rule's cell -- step, trigger and "
            "recipient -- or a mail type would send, rendered with the real "
            "templates and the call's own wording, on a synthetic proposal "
            "of the call: subject, text and HTML. Nothing is sent or kept, "
            "and no real person is named. For those who configure the call, "
            "staff and support."
        ),
        parameters=[serializers.NotificationPreviewParametersSerializer],
        request=None,
        responses={200: serializers.NotificationPreviewSerializer},
    )
    @decorators.action(
        detail=True, methods=["get"], throttle_classes=[WorkflowDryRunThrottle]
    )
    def notification_preview(self, request, uuid=None):
        call = self._workflow_reader_call()
        params = serializers.NotificationPreviewParametersSerializer(
            data=request.query_params
        )
        params.is_valid(raise_exception=True)
        return self._notification_preview_response(call, params.validated_data)

    @extend_schema(
        summary="Preview a draft of the call's wording of a mail",
        description=(
            "As notification_preview, in the wording given -- subject, text "
            "body and HTML body -- which is validated in the sandbox and not "
            "saved. For those who may update the call."
        ),
        request=serializers.NotificationPreviewDraftSerializer,
        responses={200: serializers.NotificationPreviewSerializer},
    )
    @decorators.action(
        detail=True, methods=["post"], throttle_classes=[WorkflowDryRunThrottle]
    )
    def notification_preview_draft(self, request, uuid=None):
        call = self.get_object()
        params = serializers.NotificationPreviewDraftSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        draft = {
            "subject": data["subject"],
            "body_text": data["body_text"],
            "body_html": data.get("body_html", ""),
        }
        return self._notification_preview_response(call, data, draft=draft)

    notification_preview_draft_serializer_class = (
        serializers.NotificationPreviewDraftSerializer
    )

    @extend_schema(
        summary="Import call configuration",
        description=(
            "Create a draft call under the given call managing organization "
            "from a document produced by export_call. References that cannot "
            "be resolved by name on this portal are skipped and reported in "
            "warnings. The import is atomic. A document's lists are bounded "
            "(at most 100 workflow steps, 50 notification rules a step, and "
            "so on), and imports are throttled per user."
        ),
        request=serializers.CallImportParametersSerializer,
        responses={201: serializers.CallImportResponseSerializer},
    )
    @decorators.action(
        detail=False, methods=["post"], throttle_classes=[CallImportThrottle]
    )
    def import_call(self, request):
        params = serializers.CallImportParametersSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        params.is_valid(raise_exception=True)
        template = params.validated_data.get("template")
        with transaction.atomic():
            call, imported_sections, warnings = call_transfer.import_call(
                params.validated_data["call_data"],
                manager=params.validated_data["manager"],
                user=request.user,
                sections=params.sections(),
                name=params.validated_data.get("name"),
            )
            if template is not None:
                call.template = template
                call.template_version = template.version
                call.save(update_fields=["template", "template_version"])
                workflow_versions.record_created_from_template(
                    call, template, request.user
                )
            else:
                workflow_versions.record(
                    call, request.user, workflow_versions.Reasons.IMPORTED
                )
        return response.Response(
            serializers.CallImportResponseSerializer(
                {
                    "call_uuid": call.uuid,
                    "call_name": call.name,
                    "imported_sections": imported_sections,
                    "warnings": warnings,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )

    # No object to check on the list route; CREATE_CALL on the target manager
    # is enforced by CallImportParametersSerializer.validate_manager, the same
    # split as create_permissions above.
    import_call_permissions = []
    import_call_serializer_class = serializers.CallImportParametersSerializer

    @extend_schema(
        summary="Save call configuration as a template",
        description=(
            "Save the call's settings and workflow -- proposal roles, steps, "
            "notification rules and the steps' forms -- as a new, unpublished "
            "call template. Rounds, requested offerings and documents are "
            "left out. Staff only: the template carries the call's forms, "
            "which publishing it shows to every call manager."
        ),
        request=serializers.CallSaveAsTemplateSerializer,
        responses={201: serializers.CallTemplateSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def save_as_template(self, request, uuid=None):
        call = self.get_object()
        params = serializers.CallSaveAsTemplateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        document, _ = call_transfer.export_call(call, call_templates.EXCLUDED_SECTIONS)
        template = serializers.CallTemplateSerializer(
            data={
                "name": params.validated_data.get("name") or call.name,
                "description": params.validated_data.get(
                    "description", call.description
                ),
                "document": document,
            },
            context=self.get_serializer_context(),
        )
        template.is_valid(raise_exception=True)
        saved = call_templates.save_unique(
            lambda: template.save(created_by=request.user)
        )
        workflow_versions.record_on_commit(
            saved, request.user, workflow_versions.Reasons.CREATED
        )
        return response.Response(template.data, status=status.HTTP_201_CREATED)

    save_as_template_permissions = [structure_permissions.is_staff]
    save_as_template_serializer_class = serializers.CallSaveAsTemplateSerializer

    archive_validators = [
        core_validators.StateValidator(CallStates.DRAFT, CallStates.ACTIVE)
    ]

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_rounds_list",
        request=None,
        responses=serializers.ProtectedRoundSerializer(many=True),
        description="List rounds for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_rounds_set",
        request=serializers.ProtectedRoundSerializer,
        responses=serializers.ProtectedRoundSerializer,
        description="Create a round for a call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def rounds(self, request, uuid=None):
        # TODO: Will be better move this to method of serializer and add tests.
        call: models.Call = self.get_object()
        method = self.request.method

        if method == "POST":
            if call.state == CallStates.ARCHIVED:
                raise IncorrectStateException()
            repeat = request.query_params.get("repeat", "false")
            count = request.query_params.get("count", "1")

            if repeat in ["true", "True"] and int(count) > 1:
                cutoff_time_str = request.data.get("cutoff_time")
                start_time_str = request.data.get("start_time")
                if not cutoff_time_str or not start_time_str:
                    raise exceptions.ValidationError(
                        {"cutoff_time": _("Repeated rounds need a cut-off.")}
                    )

                cutoff_time = datetime.strptime(cutoff_time_str, "%Y-%m-%dT%H:%M")
                start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M")

                duration = cutoff_time - start_time
                data = request.data.copy()
                all_created_data = []

                for i in range(int(count)):
                    new_start_time = start_time + i * duration
                    new_cutoff_time = cutoff_time + i * duration

                    data["start_time"] = new_start_time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    data["cutoff_time"] = new_cutoff_time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z"
                    )

                    serializer = self.get_serializer(
                        context=self.get_serializer_context(),
                        data=data,
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save(call=call)
                    all_created_data.append(serializer.data)
                    logger.info(
                        f"Round is created with start_time: {new_start_time}, cutoff_time: {new_cutoff_time}"
                    )
                return response.Response(
                    all_created_data,
                    status=status.HTTP_201_CREATED,
                )
            else:
                serializer = self.get_serializer(
                    context=self.get_serializer_context(),
                    data=self.request.data,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save(call=call)
                return response.Response(
                    serializer.data,
                    status=status.HTTP_201_CREATED,
                )
        queryset = (
            call.round_set.all()
            .select_related("results_published_by")
            .prefetch_related(
                # What ProtectedRoundSerializer.get_proposals reads per row.
                Prefetch(
                    "proposal_set",
                    queryset=models.Proposal.objects.select_related(
                        "round__call", "created_by", "approved_by"
                    ),
                )
            )
            .annotate(
                held_decisions_count=Count(
                    "proposal", filter=Q(proposal__decision_held=True)
                ),
                proposals_count=Count("proposal"),
            )
            .order_by("-start_time")
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, context=self.get_serializer_context(), many=True
            )
            return self.get_paginated_response(serializer.data)
        return response.Response(
            self.get_serializer(
                queryset,
                context=self.get_serializer_context(),
                many=True,
            ).data,
            status=status.HTTP_200_OK,
        )

    rounds_serializer_class = serializers.ProtectedRoundSerializer

    @extend_schema(
        operation_id="proposal_protected_calls_rounds_bulk_set",
        description=(
            "Create multiple rounds on a call at a fixed cadence. Spacing is "
            "controlled by ``cadence`` (monthly/quarterly/biannual/yearly/"
            "custom). Each round's ``cutoff_time`` is derived as "
            "``start_time + submission_window_days``. Fixed-date allocation "
            "is not supported in bulk mode."
        ),
        request=serializers.BulkRoundCreateRequestSerializer,
        responses={201: serializers.ProtectedRoundSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["post"], url_path="rounds-bulk-set")
    def rounds_bulk_set(self, request, uuid=None):
        call: models.Call = self.get_object()
        if call.state == CallStates.ARCHIVED:
            raise IncorrectStateException()
        payload = serializers.BulkRoundCreateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        rounds = utils.bulk_create_rounds(call, payload.validated_data)
        return response.Response(
            serializers.ProtectedRoundSerializer(
                rounds, many=True, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    rounds_bulk_set_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]
    rounds_bulk_set_serializer_class = serializers.BulkRoundCreateRequestSerializer

    @extend_schema(responses={status.HTTP_200_OK: serializers.ProtectedRoundSerializer})
    def round_detail(self, request, uuid=None, obj_uuid=None):
        def validate_call_state(call_round):
            if call_round.call.state == CallStates.ARCHIVED:
                raise IncorrectStateException()

        def validate_existing_of_proposals(call_round):
            # Deleting a round deletes its proposals with it. Rejected and
            # cancelled ones are outcomes of the round (a published rejection,
            # a draft its closing cancelled), so they keep the round as well.
            if call_round.proposal_set.exists():
                raise IncorrectStateException(
                    _("A round that has proposals cannot be deleted.")
                )
            # Its pools would go with it, and with them what the call
            # awards there.
            if call_round.resource_pools.exists():
                raise IncorrectStateException(
                    _("Remove the round's resource pools first.")
                )

        return self.action_detail_method(
            "round_set",
            delete_validators=[validate_call_state, validate_existing_of_proposals],
            update_validators=[validate_call_state],
        )(self, request, uuid, obj_uuid)

    round_detail_serializer_class = serializers.ProtectedRoundSerializer

    @extend_schema(responses={status.HTTP_200_OK: OpenApiTypes.STR})
    def close_round(self, request, uuid=None, obj_uuid=None):
        call: models.Call = self.get_object()

        try:
            call_round = call.round_set.get(uuid=obj_uuid)
        except models.Round.DoesNotExist:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

        if call_round.call.state != CallStates.ACTIVE:
            raise exceptions.ValidationError(_("Call is not active."))

        # Whether a round is open is derived from its times alone, so closing it
        # means moving its cutoff to now -- and saving it. The status is checked
        # under the row lock, so two concurrent closes cannot both pass.
        with transaction.atomic():
            call_round = models.Round.objects.select_for_update().get(pk=call_round.pk)
            round_status = call_round.status
            if round_status == RoundStatuses.ENDED:
                raise exceptions.ValidationError(_("Round is already closed."))
            if round_status == RoundStatuses.SCHEDULED:
                # A round that has not started holds no proposals. Closing it
                # would leave a round whose cutoff is not after its start and
                # which overlaps whichever round is open now -- both refused
                # when a round is created, edited or imported.
                raise exceptions.ValidationError(
                    _("Round has not started yet. Delete it instead of closing it.")
                )
            call_round.cutoff_time = timezone.now()
            call_round.save(update_fields=["cutoff_time"])

        utils.process_closed_round(
            call_round, announce=tasks.announce_carried_over_drafts
        )
        utils.start_round_evaluation(call_round)
        # A call evaluating at the cut-off holds its proposals until then;
        # closing the round is that cut-off, so they start now rather than at
        # the next periodic sweep.
        tasks.start_held_proposals(call_round)

        return response.Response(
            "Round has been closed.",
            status=status.HTTP_200_OK,
        )

    # CLOSE_ROUNDS, not UPDATE_CALL. Declared so the blanket unsafe-method gate
    # does not also demand UPDATE_CALL here.
    close_round_permissions = [
        permission_factory(PermissionEnum.CLOSE_ROUNDS, CALL_PERMISSION_SOURCES)
    ]

    def _get_round(self, obj_uuid):
        call: models.Call = self.get_object()
        call_round = get_object_or_404(call.round_set, uuid=obj_uuid)
        if call.state != CallStates.ACTIVE:
            raise exceptions.ValidationError(_("Call is not active."))
        return call_round

    def _round_response(self, call_round):
        call_round.refresh_from_db()
        return response.Response(
            serializers.ProtectedRoundSerializer(
                call_round, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.ProtectedRoundSerializer},
        description=(
            "Move an ended round from evaluating to deciding: its proposals "
            "have been evaluated and go to the decision body."
        ),
    )
    def start_deciding_round(self, request, uuid=None, obj_uuid=None):
        call_round = round_lifecycle.start_deciding(self._get_round(obj_uuid))
        return self._round_response(call_round)

    @extend_schema(
        request=serializers.PublishRoundResultsSerializer,
        responses={
            status.HTTP_200_OK: serializers.PublishRoundResultsResponseSerializer,
            status.HTTP_400_BAD_REQUEST: serializers.PublishRoundResultsRefusalSerializer,
        },
        description=(
            "Publish all allocation decisions of an ended round at once: "
            "announce every held decision to its applicant and carry it out. "
            "Refused while a proposal of the round has no decision, unless "
            "forced with a reason; that refusal carries undecided_count. A "
            "decision that cannot be carried out stays held and is listed in "
            "failed_proposals; publishing again retries it."
        ),
    )
    def publish_results_round(self, request, uuid=None, obj_uuid=None):
        call_round = self._get_round(obj_uuid)
        serializer = serializers.PublishRoundResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            call_round, failed = round_lifecycle.publish_results(
                call_round,
                request.user,
                force=serializer.validated_data["force"],
                reason=serializer.validated_data["reason"],
            )
        except round_lifecycle.UndecidedProposalsError as e:
            return response.Response(
                {"detail": e.detail[0], "undecided_count": e.undecided_count},
                status=status.HTTP_400_BAD_REQUEST,
            )
        call_round.refresh_from_db()
        call_round.failed_proposals = failed
        return response.Response(
            serializers.PublishRoundResultsResponseSerializer(
                call_round, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses={
            status.HTTP_200_OK: serializers.CompleteRoundResponseSerializer,
            status.HTTP_400_BAD_REQUEST: serializers.CompleteRoundRefusalSerializer,
        },
        description=(
            "Close a round whose results have been published. Refused while "
            "a decision of the round is still held because its release "
            "failed (the refusal carries held_decisions_count; publish the "
            "results again to retry it). Every proposal "
            "of the round must have a decision. What happens to those that "
            "have none follows the round's undecided_at_round_completion, or "
            "else the call's: refuse (the refusal carries undecided_count) or "
            "reject each at its current step, listed in rejected_proposals. "
            "A rejection that fails is listed in failed_proposals and the "
            "round stays open; completing again retries it."
        ),
    )
    def complete_round(self, request, uuid=None, obj_uuid=None):
        try:
            call_round, rejected, failed = round_lifecycle.complete(
                self._get_round(obj_uuid), request.user
            )
        except round_lifecycle.UndecidedAtCompletionError as e:
            return response.Response(
                {"detail": e.detail[0], "undecided_count": e.undecided_count},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except round_lifecycle.HeldDecisionsAtCompletionError as e:
            return response.Response(
                {
                    "detail": e.detail[0],
                    "held_decisions_count": e.held_decisions_count,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        call_round.refresh_from_db()
        call_round.rejected_proposals = rejected
        call_round.failed_proposals = failed
        return response.Response(
            serializers.CompleteRoundResponseSerializer(
                call_round, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request={"multipart/form-data": serializers.RoundAdoptionSerializer},
        responses={status.HTTP_200_OK: serializers.ProtectedRoundSerializer},
        description=(
            "Record that the round's results were adopted: the date, a note "
            "and optionally the adopting document. A call with "
            "adoption_required takes it only while the round is deciding, "
            "and publishes the round's results only once it is recorded. "
            "While the round withholds its results, only those who may read "
            "the adoption record may write it."
        ),
    )
    def record_adoption_round(self, request, uuid=None, obj_uuid=None):
        call_round = self._get_round(obj_uuid)
        if call_round.status != RoundStatuses.ENDED:
            raise exceptions.ValidationError(
                _("The round has not reached its cut-off yet.")
            )
        # A writer who cannot read the record would save over a note they
        # were never shown.
        if not proposal_permissions.user_can_view_round_adoption(
            request.user, call_round
        ):
            raise exceptions.PermissionDenied(
                _(
                    "Only those who may see the round's held decisions record "
                    "its adoption before its results are published."
                )
            )
        serializer = serializers.RoundAdoptionSerializer(call_round, data=request.data)
        serializer.is_valid(raise_exception=True)
        round_lifecycle.record_adoption(
            call_round, request.user, serializer.validated_data
        )
        return self._round_response(call_round)

    @extend_schema(
        request=serializers.CompleteRoundWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.CompleteRoundWorkflowStepResponseSerializer
        },
        description=(
            "Complete a call step -- named by step_uuid or its key -- with one "
            "outcome for every proposal of the round where that step is "
            "active, each as if completed on the proposal: same gates, "
            "notifications and ledger. Each proposal is committed on its own: "
            "one whose step cannot be completed, for whatever reason, is "
            "rolled back alone and listed in failed with the reason, and the "
            "rest of the round goes on. Proposals not at the step are listed "
            "in skipped. Running it again changes nothing for proposals "
            "already completed. The allocation decision is refused: it is "
            "completed on each proposal. Feedback per proposal is stored as "
            "its outcome reason for the step; on a step whose report is "
            "released to the applicant it is withheld until then, as when "
            "the step is completed on the proposal."
        ),
    )
    def complete_workflow_step_round(self, request, uuid=None, obj_uuid=None):
        call: models.Call = self.get_object()
        call_round = get_object_or_404(call.round_set, uuid=obj_uuid)
        serializer = serializers.CompleteRoundWorkflowStepSerializer(
            data=request.data, context={"round": call_round}
        )
        serializer.is_valid(raise_exception=True)
        call_step = serializer.validated_data["call_step"]
        outcome = serializer.validated_data["outcome"]
        completed, skipped, failed = round_lifecycle.complete_step_for_round(
            call_round,
            call_step,
            outcome,
            serializer.validated_data["feedback_by_proposal"],
            request.user,
        )
        if not (
            request.user.is_staff
            or proposal_permissions.manages_call_workflow(request.user, call)
        ):
            # A completer who does not manage the call hears only of the
            # proposals they act on, not of the rest of the round.
            skipped = []
        return response.Response(
            serializers.CompleteRoundWorkflowStepResponseSerializer(
                {
                    "step_uuid": call_step.uuid,
                    "step_name": call_step.name or call_step.key,
                    "outcome": outcome,
                    "completed": completed,
                    "skipped": skipped,
                    "failed": failed,
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.OpenRoundWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.OpenRoundWorkflowStepResponseSerializer,
            status.HTTP_400_BAD_REQUEST: serializers.LaggingProposalsRefusalSerializer,
        },
        description=(
            "Open a step run once per round (round_scoped) for the round, "
            "named by step_uuid or its key. Refused while a proposal of the "
            "round under evaluation has not reached the step yet, unless "
            "forced with a reason; that refusal carries lagging_count. The "
            "proposals parked on the step before it, waiting to be moved on, "
            "are moved into it, each committed on its own and listed in "
            "advanced, skipped or failed. Each contributor of the step gets a "
            "round report covering the proposals they hold it for, and one "
            "mail; the call's managers get one. due_at sets when the step is "
            "due in the round. Running it again moves in proposals that "
            "arrived meanwhile and changes nothing else."
        ),
    )
    def open_workflow_step_round(self, request, uuid=None, obj_uuid=None):
        call_round = self._get_round(obj_uuid)
        serializer = serializers.OpenRoundWorkflowStepSerializer(
            data=request.data, context={"round": call_round}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        call_step = data["call_step"]
        try:
            round_step, created, advanced, skipped, failed = round_steps.open_step(
                call_round,
                call_step,
                request.user,
                due_at=data.get("due_at"),
                force=data["force"],
                reason=data["reason"],
            )
        except round_steps.LaggingProposalsError as e:
            return response.Response(
                {"detail": e.detail[0], "lagging_count": e.lagging_count},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return response.Response(
            serializers.OpenRoundWorkflowStepResponseSerializer(
                {
                    "step_uuid": call_step.uuid,
                    "step_name": call_step.name or call_step.key,
                    "opened_at": round_step.opened_at,
                    "due_at": round_step.due_at,
                    "reports_created": len(created),
                    "advanced": advanced,
                    "skipped": skipped,
                    "failed": failed,
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.AdvanceRoundWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.AdvanceRoundWorkflowStepResponseSerializer
        },
        description=(
            "Move on every proposal of the round parked on a completed step "
            "of manual transition, named by step_uuid or its key, as the "
            "manual advance on each proposal would. Each proposal is "
            "committed on its own: one that cannot be moved is listed in "
            "failed, the rest go on. Running it again changes nothing."
        ),
    )
    def advance_workflow_step_round(self, request, uuid=None, obj_uuid=None):
        call_round = self._get_round(obj_uuid)
        serializer = serializers.AdvanceRoundWorkflowStepSerializer(
            data=request.data, context={"round": call_round}
        )
        serializer.is_valid(raise_exception=True)
        call_step = serializer.validated_data["call_step"]
        advanced, skipped, failed = round_steps.advance_for_round(
            call_round, call_step, request.user
        )
        return response.Response(
            serializers.AdvanceRoundWorkflowStepResponseSerializer(
                {
                    "step_uuid": call_step.uuid,
                    "step_name": call_step.name or call_step.key,
                    "advanced": advanced,
                    "skipped": skipped,
                    "failed": failed,
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.RoundWorkflowStepDueDateSerializer,
        responses={status.HTTP_200_OK: serializers.RoundStepSerializer(many=True)},
        description=(
            "Set when a step of the call is due in this round, for every "
            "proposal of it, or clear it (null) to count the step's duration "
            "again. The proposals at the step get the new deadline at once, "
            "and their deadline reminders are sent again for it. Returns the "
            "round's steps."
        ),
    )
    def set_workflow_step_due_date_round(self, request, uuid=None, obj_uuid=None):
        call: models.Call = self.get_object()
        call_round = get_object_or_404(call.round_set, uuid=obj_uuid)
        serializer = serializers.RoundWorkflowStepDueDateSerializer(
            data=request.data, context={"round": call_round}
        )
        serializer.is_valid(raise_exception=True)
        round_steps.set_due_date(
            call_round,
            serializer.validated_data["call_step"],
            serializer.validated_data["due_at"],
        )
        return response.Response(
            serializers.RoundStepSerializer(
                round_reports.round_steps_of(call_round), many=True
            ).data,
            status=status.HTTP_200_OK,
        )

    # Each proposal is completed, or moved on, in its own transaction, so one
    # that fails is reported and rolled back alone while the rest of the round
    # goes on.
    non_atomic_actions = (
        "complete_workflow_step_round",
        "open_workflow_step_round",
        "advance_workflow_step_round",
        "allocate_in_submission_order_round",
    )
    complete_workflow_step_round_permissions = [
        proposal_permissions.can_complete_workflow_step_round
    ]
    complete_workflow_step_round_serializer_class = (
        serializers.CompleteRoundWorkflowStepSerializer
    )
    open_workflow_step_round_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]
    open_workflow_step_round_serializer_class = (
        serializers.OpenRoundWorkflowStepSerializer
    )
    advance_workflow_step_round_permissions = open_workflow_step_round_permissions
    advance_workflow_step_round_serializer_class = (
        serializers.AdvanceRoundWorkflowStepSerializer
    )
    set_workflow_step_due_date_round_permissions = open_workflow_step_round_permissions
    set_workflow_step_due_date_round_serializer_class = (
        serializers.RoundWorkflowStepDueDateSerializer
    )

    @extend_schema(
        request=serializers.AllocateInSubmissionOrderSerializer,
        responses={
            status.HTTP_200_OK: serializers.AllocateInSubmissionOrderResponseSerializer
        },
        description=(
            "Take the allocation decision of every proposal of an ended round "
            "at it, first come first served: in submission order, approve "
            "each while its award fits what remains of every budget it draws "
            "on; decline those below the decision's quality threshold, and "
            "those that no longer fit (or leave them, on_depleted=leave) -- a "
            "later, smaller proposal still gets its chance. With preview "
            "(the default) only the plan is returned. Refused while a "
            "proposal of the round has not reached the decision, unless "
            "forced with a reason (the refusal carries waiting_count). "
            "Proposals the caller applies with or has a conflict of interest "
            "with are skipped. Each decision is taken on its own as if taken "
            "on the proposal; one that fails is listed in failed."
        ),
    )
    def allocate_in_submission_order_round(self, request, uuid=None, obj_uuid=None):
        call_round = self._get_round(obj_uuid)
        if not rankings.can_read(request.user, call_round):
            raise exceptions.PermissionDenied(
                _(
                    "The allocation plan places every proposal of the round: "
                    "not while on the applicant team of, or in conflict with, "
                    "one of them."
                )
            )
        serializer = serializers.AllocateInSubmissionOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = round_lifecycle.allocate_in_submission_order(
                call_round, request.user, **serializer.validated_data
            )
        except round_lifecycle.NotReadyForAllocationError as e:
            return response.Response(
                {"detail": e.detail[0], "waiting_count": e.waiting_count},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return response.Response(
            serializers.AllocateInSubmissionOrderResponseSerializer(result).data,
            status=status.HTTP_200_OK,
        )

    allocate_in_submission_order_round_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]
    allocate_in_submission_order_round_serializer_class = (
        serializers.AllocateInSubmissionOrderSerializer
    )

    # The round's lifecycle is round management, like closing it.
    start_deciding_round_permissions = close_round_permissions
    publish_results_round_permissions = close_round_permissions
    complete_round_permissions = close_round_permissions
    record_adoption_round_permissions = close_round_permissions
    record_adoption_round_serializer_class = serializers.RoundAdoptionSerializer
    publish_results_round_serializer_class = serializers.PublishRoundResultsSerializer

    @extend_schema(
        request=serializers.CallAttachDocumentsSerializer,
        responses=None,
        description="Attach documents to call.",
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_documents(self, request, uuid=None):
        instance: models.Call = self.get_object()

        if instance.state == CallStates.ARCHIVED:
            raise IncorrectStateException()

        # Validate through the serializer the schema already advertises.
        # Reading request.data directly let any string be written straight into
        # CallDocument.file, so a caller could store an arbitrary storage path
        # instead of an upload.
        serializer = serializers.CallAttachDocumentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        documents = serializer.validated_data["documents"]
        description = serializer.validated_data.get("description", "")

        for file_data in documents:
            obj, created = models.CallDocument.objects.get_or_create(
                call=instance,
                file=file_data,
                description=description,
            )
            if created:
                instance.documents.add(obj)
                event_logger.emit(
                    f"Attachment for call {instance.name} has been added.",
                    event_type=EventType.CALL_DOCUMENT_ADDED,
                    event_context={"call": instance},
                    scopes=[_get_customer(instance)],
                )
                logger.info(f"Attachment for {instance.name} has been added.")

        return response.Response(
            "Documents attached successfully",
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.CallDetachDocumentsSerializer,
        responses=None,
        description="Detach documents from call.",
    )
    @decorators.action(detail=True, methods=["post"])
    def detach_documents(self, request, uuid=None):
        instance: models.Call = self.get_object()
        if instance.state == CallStates.ARCHIVED:
            raise IncorrectStateException()
        serializer = serializers.CallDetachDocumentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        documents = serializer.validated_data["documents"]
        for document_uuid in documents:
            try:
                document = models.CallDocument.objects.get(
                    call=instance,
                    uuid=document_uuid,
                )
            except models.CallDocument.DoesNotExist:
                raise exceptions.NotFound(
                    f"Document {document_uuid} is not attached to this call."
                )
            document.delete()
            event_logger.emit(
                f"Attachment for call {instance.name} has been removed.",
                event_type=EventType.CALL_DOCUMENT_REMOVED,
                event_context={"call": instance},
                scopes=[_get_customer(instance)],
            )
            logger.info(f"Attachment for {instance.name} has been removed.")

        return response.Response(
            "Documents removed successfully",
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_resource_templates_list",
        request=None,
        responses=serializers.CallResourceTemplateSerializer(many=True),
        description="List resource templates for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_resource_templates_set",
        request=serializers.CallResourceTemplateSerializer,
        responses=serializers.CallResourceTemplateSerializer,
        description="Create resource template for a call.",
    )
    @extend_schema(responses={status.HTTP_200_OK: dict})
    @decorators.action(detail=True, methods=["get", "post"])
    def resource_templates(self, request, uuid=None):
        return self.action_list_method(
            "resource_templates",
            additional_validators=["validate_call_not_archived"],
        )(self, request, uuid)

    resource_templates_serializer_class = serializers.CallResourceTemplateSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallResourceTemplateSerializer}
    )
    def resource_template_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "resource_templates",
            delete_validators=[validate_call_not_archived],
            update_validators=[validate_call_not_archived],
        )(self, request, uuid, obj_uuid)

    resource_template_detail_serializer_class = (
        serializers.CallResourceTemplateSerializer
    )

    def _nested_list(self, queryset, serializer_class, call):
        """List ``queryset`` or create from the request's data, as the
        call's nested lists do: a page at a time, and refused on an archived
        call."""
        request = self.request
        context = {**self.get_serializer_context(), "call": call}
        if request.method == "POST":
            serializer = serializer_class(data=request.data, context=context)
            serializer.is_valid(raise_exception=True)
            serializer.validate_call_not_archived(call)
            try:
                with transaction.atomic():
                    serializer.save()
            except IntegrityError:
                raise exceptions.ValidationError(
                    _("It was just added by someone else.")
                )
            return response.Response(serializer.data, status=status.HTTP_201_CREATED)
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                serializer_class(page, many=True, context=context).data
            )
        return response.Response(
            serializer_class(queryset, many=True, context=context).data,
            status=status.HTTP_200_OK,
        )

    def _call_rules(self, call):
        return (
            models.CallOfferingComponent.objects.filter(requested_offering__call=call)
            .select_related("requested_offering__offering", "requested_offering__call")
            .order_by("requested_offering_id", "created", "id")
        )

    def _call_pools(self, call):
        return budgets.call_pools(call).select_related(
            "offering_component__requested_offering__call"
        )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_offering_components_list",
        request=None,
        responses=serializers.CallOfferingComponentSerializer(many=True),
        description=(
            "List the call's component rules: per component of a call "
            "offering, what a proposal may request of it."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_offering_components_set",
        request=serializers.CallOfferingComponentSerializer,
        responses=serializers.CallOfferingComponentSerializer,
        description=(
            "Set what a proposal may request of a component of a call "
            "offering: one billed by its limit that the offering's plan "
            "prices. Refused once a submitted proposal requests the offering."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def offering_components(self, request, uuid=None):
        call = self.get_object()
        return self._nested_list(
            self._call_rules(call), serializers.CallOfferingComponentSerializer, call
        )

    offering_components_serializer_class = serializers.CallOfferingComponentSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallOfferingComponentSerializer}
    )
    def offering_component_detail(self, request, uuid=None, obj_uuid=None):
        call = self.get_object()
        try:
            rule = self._call_rules(call).get(uuid=obj_uuid)
        except (models.CallOfferingComponent.DoesNotExist, ValueError):
            return response.Response(status=status.HTTP_404_NOT_FOUND)
        context = {**self.get_serializer_context(), "call": call}
        if request.method == "GET":
            return response.Response(
                serializers.CallOfferingComponentSerializer(rule, context=context).data
            )
        validate_call_not_archived(rule.requested_offering)
        if request.method == "DELETE":
            if budgets.rules_locked(rule.requested_offering):
                raise IncorrectStateException(
                    _(
                        "A submitted proposal requests this offering: its "
                        "component rules can no longer change."
                    )
                )
            if rule.pools.exists():
                raise IncorrectStateException(
                    _(
                        "The component has resource pools. Remove them before "
                        "removing its rule."
                    )
                )
            rule.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        serializer = serializers.CallOfferingComponentSerializer(
            rule, data=request.data, partial=True, context=context
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    offering_component_detail_serializer_class = (
        serializers.CallOfferingComponentSerializer
    )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_resource_pools_list",
        request=None,
        responses=serializers.CallResourcePoolSerializer(many=True),
        description=(
            "List the call's resource pools: per component rule, how much "
            "the call awards over the whole call or one round."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_resource_pools_set",
        request=serializers.CallResourcePoolSerializer,
        responses=serializers.CallResourcePoolSerializer,
        description=(
            "Add a resource pool to a component rule of the call: for the "
            "whole call (no round) or one round. A proposal draws on both."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def resource_pools(self, request, uuid=None):
        call = self.get_object()
        return self._nested_list(
            self._call_pools(call).order_by("created", "id"),
            serializers.CallResourcePoolSerializer,
            call,
        )

    resource_pools_serializer_class = serializers.CallResourcePoolSerializer
    resource_pools_permissions = [proposal_permissions.can_manage_call_budgets]

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallResourcePoolSerializer}
    )
    def resource_pool_detail(self, request, uuid=None, obj_uuid=None):
        call = self.get_object()
        try:
            pool = self._call_pools(call).get(uuid=obj_uuid)
        except (models.CallResourcePool.DoesNotExist, ValueError):
            return response.Response(status=status.HTTP_404_NOT_FOUND)
        context = {**self.get_serializer_context(), "call": call}
        if request.method == "GET":
            return response.Response(
                serializers.CallResourcePoolSerializer(pool, context=context).data
            )
        validate_call_not_archived(pool.offering_component.requested_offering)
        if request.method == "DELETE":
            pool.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        serializer = serializers.CallResourcePoolSerializer(
            pool, data=request.data, partial=True, context=context
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    resource_pool_detail_serializer_class = serializers.CallResourcePoolSerializer
    resource_pool_detail_permissions = [proposal_permissions.can_manage_call_budgets]

    @extend_schema(
        responses={200: serializers.ResourcePoolUsageSerializer(many=True)},
        description=(
            "The call's resource pools as a matrix: per component rule, a "
            "cell for the whole call and one per round, each with what its "
            "pool offers and what was requested, awarded and remains. "
            "Awarded counts what the allocation decisions granted, held and "
            "tentative awards included. For the call's managers, staff and "
            "support."
        ),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def resource_pool_usage(self, request, uuid=None):
        call = self.get_object()
        return response.Response(
            serializers.ResourcePoolUsageSerializer(
                budgets.call_usage(
                    call,
                    held_from=proposal_permissions.proposals_with_held_decisions_hidden_from(
                        request.user
                    ),
                ),
                many=True,
            ).data,
            status=status.HTTP_200_OK,
        )

    resource_pool_usage_permissions = [proposal_permissions.can_manage_call_budgets]

    # Workflow Step Configuration Endpoints

    # What of a step only the call's team reads: who holds its completer
    # role and who its contributor source names (with their emails), its notification rules, every audience's
    # status labels, when it runs and the score it gates on. Anyone else
    # with a role on the call reads the step as an empty value of each.
    CALL_TEAM_STEP_FIELDS = {
        "responsible_users": list,
        "contributing_users": list,
        "notification_rules": list,
        "status_labels": dict,
        "run_condition": dict,
        "run_condition_step_name": lambda: None,
        "min_score_threshold": lambda: None,
        "contributors_warning": lambda: None,
    }

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_workflow_steps_list",
        request=None,
        responses=serializers.CallWorkflowStepSerializer(many=True),
        description=(
            "List workflow steps for a call. Who holds each step's completer "
            "role, its notification rules, status labels, run condition and "
            "score threshold are the call team's: anyone else with a role on "
            "the call reads them empty."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_workflow_steps_set",
        request=serializers.CallWorkflowStepSerializer,
        responses=serializers.CallWorkflowStepSerializer,
        description="Create or update a workflow step for a call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def workflow_steps(self, request, uuid=None):
        """GET returns the call's steps in workflow order; POST delegates to
        action_list_method for the standard create path."""
        if request.method == "GET":
            call = self.get_object()
            steps = (
                call.workflow_steps.select_related("checklist", "call")
                .prefetch_related(
                    "criteria", "notification_rules__notified_proposal_roles"
                )
                .order_by("order", "id")
            )
            serializer = self.get_serializer(
                steps, context=self.get_serializer_context(), many=True
            )
            data = serializer.data
            if not _may_read_call_workflow(request.user, call):
                data = [self._step_for_evaluator(step) for step in data]
            return response.Response(data, status=status.HTTP_200_OK)
        return self.action_list_method(
            "workflow_steps",
            additional_validators=["validate_call_not_archived"],
        )(self, request, uuid)

    workflow_steps_serializer_class = serializers.CallWorkflowStepSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallWorkflowStepSerializer}
    )
    def workflow_step_detail(self, request, uuid=None, obj_uuid=None):
        result = self.action_detail_method(
            "workflow_steps",
            delete_validators=[validate_call_not_archived, validate_step_not_walked],
            update_validators=[validate_call_not_archived],
        )(self, request, uuid, obj_uuid)
        if (
            request.method == "GET"
            and result.status_code == status.HTTP_200_OK
            and not _may_read_call_workflow(request.user, self.get_object())
        ):
            result.data = self._step_for_evaluator(result.data)
        return result

    @classmethod
    def _step_for_evaluator(cls, step):
        """``step`` as read by someone with a role on the call outside its
        team: what only the team reads, empty."""
        return {
            **step,
            **{field: empty() for field, empty in cls.CALL_TEAM_STEP_FIELDS.items()},
        }

    workflow_step_detail_serializer_class = serializers.CallWorkflowStepSerializer

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_proposal_roles_list",
        request=None,
        responses=serializers.CallProposalRoleSerializer(many=True),
        description=(
            "List the evaluator roles the call gives on its proposals, such as "
            "a lead and a second rapporteur."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_proposal_roles_set",
        request=serializers.CallProposalRoleSerializer,
        responses=serializers.CallProposalRoleSerializer,
        description="Add an evaluator role to the call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def proposal_roles(self, request, uuid=None):
        return self.action_list_method(
            "proposal_roles",
            additional_validators=["validate_call_not_archived"],
        )(self, request, uuid)

    proposal_roles_serializer_class = serializers.CallProposalRoleSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallProposalRoleSerializer}
    )
    def proposal_role_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "proposal_roles",
            delete_validators=[
                validate_call_not_archived,
                validate_proposal_role_unused,
            ],
            update_validators=[validate_call_not_archived],
        )(self, request, uuid, obj_uuid)

    proposal_role_detail_serializer_class = serializers.CallProposalRoleSerializer

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_panels_list",
        request=None,
        responses=serializers.CallPanelSerializer(many=True),
        description=(
            "List the call's panels: the science domains each covers, its "
            "chair and members. For whoever may update the call and the "
            "call's panel members."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_panels_set",
        request=serializers.CallPanelSerializer,
        responses=serializers.CallPanelSerializer,
        description=(
            "Add a panel to the call. Proposals are routed to it by their "
            "science sub-domain when their evaluation starts."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def panels(self, request, uuid=None):
        return self.action_list_method(
            "panels",
            additional_validators=["validate_call_not_archived"],
        )(self, request, uuid)

    panels_serializer_class = serializers.CallPanelSerializer
    panels_extra_permissions = [proposal_permissions.can_read_call_panels]

    @extend_schema(responses={status.HTTP_200_OK: serializers.CallPanelSerializer})
    def panel_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "panels",
            delete_validators=[validate_call_not_archived, validate_panel_unused],
            update_validators=[validate_call_not_archived],
        )(self, request, uuid, obj_uuid)

    panel_detail_serializer_class = serializers.CallPanelSerializer
    panel_detail_extra_permissions = [proposal_permissions.can_read_call_panels]

    @extend_schema(
        description=(
            "List checklists that can be attached to a workflow step "
            "(WORKFLOW_STEP-typed). Available to call managers so the workflow "
            "config UI can populate its checklist picker without staff-only "
            "access to the checklist admin API. Lists the forms the whole "
            "portal shares and those of the organizations whose calls the user "
            "configures; staff and support see every form."
        ),
        parameters=[
            OpenApiParameter(
                name="customer_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description=(
                    "Only the forms a call of this organization can use: the "
                    "shared ones and the organization's own."
                ),
                required=False,
            ),
        ],
        responses={
            status.HTTP_200_OK: checklist_serializers.ChecklistShortSerializer(
                many=True
            )
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def step_checklists(self, request):
        user = request.user
        if not proposal_permissions.may_configure_calls(user):
            raise exceptions.PermissionDenied(
                _("Only those who configure calls read the step forms.")
            )
        checklists = proposal_permissions.forms_visible_to(
            user,
            checklist_models.Checklist.objects.filter(
                checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
            ),
        )
        customer_uuid = request.query_params.get("customer_uuid")
        if customer_uuid:
            try:
                customer_uuid = uuid_module.UUID(customer_uuid)
            except ValueError:
                raise exceptions.ValidationError(
                    {"customer_uuid": _("Enter a valid UUID.")}
                ) from None
            checklists = checklists.filter(
                Q(organization__isnull=True) | Q(organization__uuid=customer_uuid)
            )
        serializer = checklist_serializers.ChecklistShortSerializer(
            checklists.order_by("name"), many=True
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    # Read by the workflow step configuration only: a step's own form reaches
    # the people filling it through the proposal's step-checklist endpoints.
    step_checklists_permissions = []

    @extend_schema(
        description=(
            "List the kinds a call's workflow steps are made of: their outcome "
            "sets, default completer and the step fields that apply to them. "
            "For the workflow configuration UI."
        ),
        responses={status.HTTP_200_OK: serializers.StepKindSerializer(many=True)},
    )
    @decorators.action(detail=False, methods=["get"])
    def workflow_step_kinds(self, request):
        serializer = serializers.StepKindSerializer(
            proposal_enums.STEP_KINDS, many=True
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    # The kinds are code, the same for every call.
    workflow_step_kinds_permissions = []

    # Call Manager Compliance Endpoints
    compliance_overview_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
        )
    ]

    @extend_schema(
        description="Get compliance overview for call manager showing all proposals and their compliance status.",
        responses=serializers.CallComplianceOverviewSerializer,
    )
    @decorators.action(detail=True, methods=["get"])
    def compliance_overview(self, request, uuid=None):
        """Get compliance overview for call manager."""
        call = self.get_object()

        if not call.compliance_checklist:
            return response.Response(
                {
                    "detail": "No compliance checklist configured for this call",
                    "checklist": None,
                    "proposals": [],
                }
            )

        # Serialize the compliance overview
        overview_data = serializers.CallComplianceOverviewSerializer(
            call, context={"request": request}
        ).data

        return response.Response(overview_data)

    review_proposal_compliance_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]

    @extend_schema(
        description="Mark proposal compliance as reviewed by call manager.",
        request=serializers.CallComplianceReviewSerializer,
        responses=dict[str, str],
    )
    @decorators.action(detail=True, methods=["post"])
    def review_proposal_compliance(self, request, uuid=None):
        """Mark proposal compliance as reviewed by call manager."""
        call = self.get_object()

        # Validate input
        serializer = serializers.CallComplianceReviewSerializer(
            data=request.data, context={"call": call, "request": request}
        )
        serializer.is_valid(raise_exception=True)

        proposal_uuid = serializer.validated_data["proposal_uuid"]
        review_notes = serializer.validated_data.get("review_notes", "")

        try:
            proposal: Proposal = Proposal.objects.get(
                round__call=call, uuid=proposal_uuid
            )
            completion = proposal.checklist_completion

            completion.reviewed_by = request.user
            completion.reviewed_at = timezone.now()
            completion.review_notes = review_notes
            completion.save()

            return response.Response(
                {
                    "detail": "Compliance review completed successfully",
                    "proposal_uuid": str(proposal_uuid),
                    "reviewed_by": request.user.full_name,
                    "reviewed_at": completion.reviewed_at,
                }
            )

        except models.Proposal.DoesNotExist:
            return response.Response(
                {"detail": "Proposal not found"}, status=status.HTTP_404_NOT_FOUND
            )
        except checklist_models.ChecklistCompletion.DoesNotExist:
            return response.Response(
                {"detail": "Proposal has no compliance checklist"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    proposal_compliance_answers_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
    ]

    @extend_schema(
        description="Get detailed compliance answers for a specific proposal (call managers only).",
        responses=checklist_serializers.AnswerSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="proposal_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the proposal",
            )
        ],
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        url_path="proposals/(?P<proposal_uuid>[^/.]+)/compliance-answers",
    )
    def proposal_compliance_answers(self, request, uuid=None, proposal_uuid=None):
        """Get detailed compliance answers for a specific proposal."""
        call: models.Call = self.get_object()

        try:
            proposal = models.Proposal.objects.get(uuid=proposal_uuid, round__call=call)

            if not hasattr(proposal, "checklist_completion"):
                return response.Response(
                    {"detail": "Proposal has no compliance checklist"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            completion = proposal.checklist_completion
            answers = completion.answers.all().select_related("question", "user")

            answers_data = checklist_serializers.AnswerSerializer(
                answers, many=True, context={"request": request}
            ).data

            return response.Response(
                {
                    "proposal": {
                        "uuid": str(proposal.uuid),
                        "name": proposal.name,
                        "created_by": proposal.created_by.full_name
                        if proposal.created_by
                        else None,
                    },
                    "completion": checklist_serializers.ChecklistCompletionReviewerSerializer(
                        completion, context={"request": request}
                    ).data,
                    "answers": answers_data,
                }
            )

        except models.Proposal.DoesNotExist:
            return response.Response(
                {"detail": "Proposal not found"}, status=status.HTTP_404_NOT_FOUND
            )

    @extend_schema(
        description="Get available compliance checklists for call creation/editing.",
        responses=serializers.AvailableChecklistSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="checklist_type",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by checklist type (default: proposal_compliance)",
                required=False,
            ),
            OpenApiParameter(
                name="customer_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                description="UUID of the customer running the call managing organization whose checklists to list. The caller must configure a call of that organization or be allowed to create one there.",
                required=True,
            ),
        ],
    )
    @decorators.action(detail=False, methods=["get"])
    def available_compliance_checklists(self, request):
        """Get list of available compliance checklists for call managers/organizers."""
        customer_uuid = request.query_params.get("customer_uuid")
        if not customer_uuid:
            raise exceptions.ValidationError(
                {"customer_uuid": "This parameter is required."}
            )

        customer = (
            Customer.objects.filter(uuid=customer_uuid).first()
            if _is_uuid(customer_uuid)
            else None
        )
        if not customer:
            raise exceptions.ValidationError({"customer_uuid": "Customer not found."})

        has_call_managing_org = models.CallManagingOrganisation.objects.filter(
            customer=customer
        ).exists()
        if not has_call_managing_org:
            raise exceptions.ValidationError(
                {
                    "customer_uuid": "Customer does not have a call managing organization."
                }
            )
        checklist_type = request.query_params.get(
            "checklist_type", ChecklistTypes.PROPOSAL_COMPLIANCE
        )

        checklists = (
            checklist_models.Checklist.objects.filter(checklist_type=checklist_type)
            .filter(Q(organization__isnull=True) | Q(organization=customer))
            .prefetch_related("questions")
            .order_by("name")
        )

        serializer = serializers.AvailableChecklistSerializer(
            checklists, many=True, context={"request": request}
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @staticmethod
    def _check_available_checklists_permission(request, view, obj=None):
        """Open to whoever configures a call of the organization or may create
        one there: the people who pick a call's compliance checklist."""
        user = request.user
        if has_support_access(request):
            return

        customer_uuid = request.query_params.get("customer_uuid")
        if not customer_uuid or not _is_uuid(customer_uuid):
            return

        call_managing_org = models.CallManagingOrganisation.objects.filter(
            customer__uuid=customer_uuid
        ).first()
        if call_managing_org is None:
            # The action answers with its own 400.
            return
        # Given the request, a personal access token's scopes and bindings
        # apply.
        if permissions_utils.has_permission(
            request, PermissionEnum.CREATE_CALL, call_managing_org
        ):
            return
        # The calls the user's roles let them configure, each confirmed
        # against the request, so that a token's limits hold here as well.
        configurable_calls = models.Call.objects.filter(
            pk__in=proposal_permissions.configurable_call_ids(user),
            manager=call_managing_org,
        ).select_related("manager")
        if any(
            permissions_utils.has_permission_on_any_source(
                request,
                PermissionEnum.UPDATE_CALL,
                call,
                ["*", "manager", "manager.customer"],
            )
            for call in configurable_calls
        ):
            return
        raise exceptions.PermissionDenied(
            "You do not configure any call of this organization."
        )

    available_compliance_checklists_permissions = [
        _check_available_checklists_permission
    ]

    # =========================================================================
    # Reviewer Pool Management
    # =========================================================================

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_reviewer_pool_list",
        responses=serializers.CallReviewerPoolSerializer(many=True),
        description="List reviewer pool members for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_invite_reviewers",
        request=serializers.ReviewerInvitationSerializer,
        responses=serializers.CallReviewerPoolSerializer(many=True),
        description="Invite reviewers to join the call's reviewer pool.",
    )
    @decorators.action(detail=True, methods=["get", "post"], url_path="reviewer-pool")
    def reviewer_pool(self, request, uuid=None):
        """List or invite reviewers to the call's reviewer pool."""
        call = self.get_object()

        if request.method == "GET":
            pool_members = list(
                models.CallReviewerPool.objects.filter(call=call)
                .with_open_assignments()
                .select_related(
                    "reviewer",
                    "reviewer__user",
                    "invited_by",
                    "invited_user",
                )
            )

            # Build prefetch context to avoid N+1 in serializer
            context = self.get_serializer_context()
            context.update(self._build_pool_serializer_context(pool_members, call))

            serializer = serializers.CallReviewerPoolSerializer(
                pool_members,
                many=True,
                context=context,
            )
            return response.Response(serializer.data)

        # POST - invite reviewers
        serializer = serializers.ReviewerInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reviewer_uuids = serializer.validated_data["reviewer_uuids"]
        max_assignments = serializer.validated_data.get("max_assignments", 5)
        invitation_message = serializer.validated_data.get("invitation_message", "")

        # Bulk fetch all reviewers at once to avoid N+1
        reviewers = models.ReviewerProfile.objects.filter(
            uuid__in=reviewer_uuids
        ).select_related("user")
        reviewers_by_uuid = {r.uuid: r for r in reviewers}
        # Only a reviewer who may take on review work for this call is invited.
        refused = {}
        for reviewer in reviewers:
            error = reviewer_standing.standing_error(
                reviewer.user
            ) or reviewer_standing.eligibility_error(call, reviewer.user)
            if error:
                refused[reviewer.user.full_name] = error
        if refused:
            raise exceptions.ValidationError(
                {
                    "reviewer_uuids": [
                        f"{name}: {reason}" for name, reason in refused.items()
                    ]
                }
            )
        email_invited_ids = reviewers_with_email_invitation(call, reviewers)

        created_memberships = []
        expires_at = models.CallReviewerPool.get_invitation_expires_at(call)
        with transaction.atomic():
            for reviewer_uuid in reviewer_uuids:
                reviewer = reviewers_by_uuid.get(reviewer_uuid)
                if not reviewer or reviewer.id in email_invited_ids:
                    continue

                membership, created = models.CallReviewerPool.objects.get_or_create(
                    call=call,
                    reviewer=reviewer,
                    defaults={
                        "invited_by": request.user,
                        "max_assignments": max_assignments,
                        "invitation_expires_at": expires_at,
                    },
                )
                if created:
                    created_memberships.append(membership)
                    transaction.on_commit(
                        lambda uuid=membership.uuid: (
                            tasks.send_reviewer_invitation_email.delay(
                                uuid, invitation_message
                            )
                        )
                    )

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                created_memberships,
                many=True,
                context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def _build_pool_serializer_context(self, pool_members, call):
        """Build prefetched COI and review counts for CallReviewerPoolSerializer."""
        if not pool_members:
            return {}

        reviewer_ids = {pm.reviewer_id for pm in pool_members if pm.reviewer_id}
        user_ids = set()
        for pm in pool_members:
            if pm.reviewer and pm.reviewer.user_id:
                user_ids.add(pm.reviewer.user_id)
            elif pm.invited_user_id:
                user_ids.add(pm.invited_user_id)

        # Prefetch COI counts
        coi_counts = {}
        coi_by_severity = {}
        if reviewer_ids:
            coi_data = (
                models.ConflictOfInterest.objects.filter(
                    reviewer_id__in=reviewer_ids,
                    call=call,
                )
                .values("reviewer_id", "severity")
                .annotate(count=Count("id"))
            )
            for item in coi_data:
                key = (item["reviewer_id"], call.id)
                coi_counts[key] = coi_counts.get(key, 0) + item["count"]
                if key not in coi_by_severity:
                    coi_by_severity[key] = {}
                coi_by_severity[key][item["severity"]] = item["count"]

        # Prefetch review counts
        review_counts = {}
        if user_ids:
            review_data = (
                models.Review.objects.filter(
                    reviewer_id__in=user_ids,
                    proposal__round__call=call,
                )
                .values("reviewer_id", "state")
                .annotate(count=Count("id"))
            )
            for item in review_data:
                key = (item["reviewer_id"], call.id)
                if key not in review_counts:
                    review_counts[key] = {}
                review_counts[key][item["state"]] = item["count"]

        return {
            "coi_counts": coi_counts,
            "coi_by_severity": coi_by_severity,
            "review_counts": review_counts,
        }

    reviewer_pool_serializer_class = serializers.CallReviewerPoolSerializer
    reviewer_pool_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    # =========================================================================
    # Reviewer Discovery & Email Invitations
    # =========================================================================

    @extend_schema(
        description="Invite a reviewer by email address. Creates an invitation that requires the reviewer to create and publish a profile before accepting.",
        request=serializers.EmailInvitationSerializer,
        responses=serializers.CallReviewerPoolSerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="invite-by-email")
    def invite_by_email(self, request, uuid=None):
        """Invite a reviewer by email address."""
        call = self.get_object()

        serializer = serializers.EmailInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        max_assignments = serializer.validated_data["max_assignments"]
        invitation_message = serializer.validated_data.get("invitation_message", "")

        try:
            pool_member = reviewer_pool.invite_by_email(
                call,
                email,
                request.user,
                max_assignments=max_assignments,
                invitation_message=invitation_message,
            )
        except reviewer_pool.AlreadyInPool as exc:
            return response.Response(
                {"detail": exc.message},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                pool_member, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    invite_by_email_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Generate reviewer suggestions with configurable matching source.",
        request=serializers.GenerateSuggestionsRequestSerializer,
        responses={200: serializers.GenerateSuggestionsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="generate-suggestions")
    def generate_suggestions(self, request, uuid=None):
        """
        Run affinity algorithm on published profiles to generate suggestions.

        Request body options:
        - source: One of 'call_description', 'all_proposals', 'selected_proposals', 'custom_keywords'
        - proposal_uuids: List of proposal UUIDs (for 'selected_proposals' source)
        - keywords: List of keyword strings (for 'custom_keywords' source)
        - keyword_search_mode: 'expertise_only' or 'full_text' (for 'custom_keywords' source)
        - min_affinity_threshold: Override minimum threshold (0-1)
        """
        reviewer_standing.ensure_automated_matching_enabled()
        call = self.get_object()

        # Parse request body if provided
        if request.data:
            req_serializer = serializers.GenerateSuggestionsRequestSerializer(
                data=request.data
            )
            req_serializer.is_valid(raise_exception=True)
            data = req_serializer.validated_data

            result = affinity_scoring.compute_suggestions_for_call_configurable(
                call=call,
                source=data.get("source", "all_proposals"),
                proposal_uuids=data.get("proposal_uuids"),
                keywords=data.get("keywords"),
                keyword_search_mode=data.get("keyword_search_mode", "expertise_only"),
                min_threshold=data.get("min_affinity_threshold"),
            )
        else:
            # Backward compatibility: no body means use all_proposals
            result = affinity_scoring.compute_suggestions_for_call_configurable(
                call=call,
                source="all_proposals",
            )

        return response.Response(result, status=status.HTTP_200_OK)

    generate_suggestions_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="List all reviewer suggestions for this call with affinity scores.",
        responses=serializers.ReviewerSuggestionSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"])
    def suggestions(self, request, uuid=None):
        """List reviewer suggestions for this call."""
        call = self.get_object()
        suggestions = models.ReviewerSuggestion.objects.filter(call=call)

        # Apply status filter if provided
        status_filter = request.query_params.getlist("status")
        if status_filter:
            suggestions = suggestions.filter(status__in=status_filter)

        serializer = serializers.ReviewerSuggestionSerializer(
            suggestions,
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data)

    suggestions_serializer_class = serializers.ReviewerSuggestionSerializer
    suggestions_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        description="Send invitations to all confirmed suggestions.",
        request=None,
        responses={200: serializers.SendInvitationsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="send-invitations")
    def send_invitations(self, request, uuid=None):
        """Send invitations to all confirmed suggestions."""
        call = self.get_object()

        with transaction.atomic():
            # Lock the suggestions so a repeated request waits and then finds
            # them already invited instead of inviting them twice
            confirmed_suggestions = list(
                models.ReviewerSuggestion.objects.filter(
                    call=call,
                    status=ReviewerSuggestionStatuses.CONFIRMED,
                )
                .select_related("reviewer__user")
                .select_for_update(of=("self",))
            )

            if not confirmed_suggestions:
                return response.Response(
                    {"invitations_sent": 0},
                    status=status.HTTP_200_OK,
                )

            # Prefetch existing pool members to avoid N+1 existence checks
            existing_pool_reviewer_ids = set(
                models.CallReviewerPool.objects.filter(
                    call=call,
                    reviewer_id__in=[s.reviewer_id for s in confirmed_suggestions],
                ).values_list("reviewer_id", flat=True)
            )
            existing_pool_reviewer_ids |= reviewers_with_email_invitation(
                call, [s.reviewer for s in confirmed_suggestions]
            )

            # Prepare bulk operations
            invitations_to_create = []
            suggestions_to_update = []
            expires_at = models.CallReviewerPool.get_invitation_expires_at(call)

            skipped = []
            for suggestion in confirmed_suggestions:
                # Skip if already in pool
                if suggestion.reviewer_id in existing_pool_reviewer_ids:
                    continue
                # Re-checked here: a reviewer suspended since being suggested,
                # or outside the call's reviewer eligibility, stays uninvited.
                user = suggestion.reviewer.user
                error = reviewer_standing.standing_error(
                    user
                ) or reviewer_standing.eligibility_error(call, user)
                if error:
                    skipped.append(f"{user.full_name}: {error}")
                    continue

                # Prepare invitation for bulk create
                invitations_to_create.append(
                    models.CallReviewerPool(
                        call=call,
                        reviewer=suggestion.reviewer,
                        invited_by=request.user,
                        invitation_status=ReviewerPoolInvitationStatuses.PENDING,
                        invitation_expires_at=expires_at,
                        expertise_match_score=suggestion.affinity_score,
                    )
                )

                # Mark suggestion for update
                suggestion.status = ReviewerSuggestionStatuses.INVITED
                suggestions_to_update.append(suggestion)

            # Bulk create invitations
            if invitations_to_create:
                models.CallReviewerPool.objects.bulk_create(invitations_to_create)

            # Bulk update suggestion statuses
            if suggestions_to_update:
                models.ReviewerSuggestion.objects.bulk_update(
                    suggestions_to_update, fields=["status"]
                )

            for invitation in invitations_to_create:
                transaction.on_commit(
                    lambda uuid=invitation.uuid: (
                        tasks.send_reviewer_invitation_email.delay(uuid)
                    )
                )

        return response.Response(
            {"invitations_sent": len(invitations_to_create), "skipped": skipped},
            status=status.HTTP_200_OK,
        )

    send_invitations_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    # =========================================================================
    # COI Management
    # =========================================================================

    @extend_schema(
        description="Get COI configuration for this call.",
        responses=serializers.CallCOIConfigurationSerializer,
    )
    @decorators.action(
        detail=True, methods=["get", "patch"], url_path="coi-configuration"
    )
    def coi_configuration(self, request, uuid=None):
        """Get or update COI configuration for a call."""
        call = self.get_object()

        if request.method == "GET":
            config, _ = models.CallCOIConfiguration.objects.get_or_create(call=call)
            serializer = serializers.CallCOIConfigurationSerializer(
                config, context=self.get_serializer_context()
            )
            return response.Response(serializer.data)

        # PATCH - update configuration.
        # The mutual-exclusion rule is validated against the persisted row, so
        # the read and the write have to be one atomic, locked unit. Two
        # concurrent PATCHes each writing one half of an overlapping pair would
        # otherwise both validate against a disjoint snapshot and both commit,
        # producing the exact state this rule makes unreachable.
        with transaction.atomic():
            config, _ = models.CallCOIConfiguration.objects.get_or_create(call=call)
            config = models.CallCOIConfiguration.objects.select_for_update().get(
                pk=config.pk
            )
            serializer = serializers.CallCOIConfigurationSerializer(
                config,
                data=request.data,
                partial=True,
                context=self.get_serializer_context(),
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return response.Response(serializer.data)

    coi_configuration_serializer_class = serializers.CallCOIConfigurationSerializer
    coi_configuration_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        description="List all conflicts of interest detected for this call.",
        responses=serializers.ConflictOfInterestSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"])
    def conflicts(self, request, uuid=None):
        """List all COIs for a call."""
        call = self.get_object()
        conflicts = models.ConflictOfInterest.objects.filter(call=call).order_by(
            "-detected_at"
        )

        # Apply filters from query params
        status_filter = request.query_params.getlist("status")
        if status_filter:
            conflicts = conflicts.filter(status__in=status_filter)

        severity_filter = request.query_params.get("severity")
        if severity_filter:
            conflicts = conflicts.filter(severity=severity_filter)

        serializer = serializers.ConflictOfInterestSerializer(
            conflicts,
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data)

    conflicts_serializer_class = serializers.ConflictOfInterestSerializer
    conflicts_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        description="Get summary statistics of conflicts for this call.",
        responses={200: serializers.ConflictSummaryResponseSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="conflict-summary")
    def conflict_summary(self, request, uuid=None):
        """Get summary statistics of COIs for a call."""
        call = self.get_object()

        # Use aggregation queries instead of iterating queryset multiple times
        # Count by status
        status_counts = (
            models.ConflictOfInterest.objects.filter(call=call)
            .values("status")
            .annotate(count=Count("id"))
        )
        by_status = {item["status"]: item["count"] for item in status_counts}

        # Count by severity
        severity_counts = (
            models.ConflictOfInterest.objects.filter(call=call)
            .values("severity")
            .annotate(count=Count("id"))
        )
        by_severity = {item["severity"]: item["count"] for item in severity_counts}

        # Count by type
        type_counts = (
            models.ConflictOfInterest.objects.filter(call=call)
            .values("coi_type")
            .annotate(count=Count("id"))
        )
        by_type = {item["coi_type"]: item["count"] for item in type_counts}

        # Calculate total
        total = sum(by_status.values())

        return response.Response(
            {
                "total": total,
                "by_status": by_status,
                "by_severity": by_severity,
                "by_type": by_type,
            }
        )

    conflict_summary_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        description="Trigger automated COI detection for all reviewer-proposal pairs.",
        request=serializers.TriggerCOIDetectionSerializer,
        responses=serializers.COIDetectionJobSerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="detect-conflicts")
    def detect_conflicts(self, request, uuid=None):
        """Trigger COI detection job for this call."""
        reviewer_standing.ensure_coi_detection_enabled()
        call = self.get_object()

        serializer = serializers.TriggerCOIDetectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        job_type = serializer.validated_data.get(
            "job_type", COIDetectionJobTypes.FULL_CALL
        )

        # Create detection job
        job = models.COIDetectionJob.objects.create(
            call=call,
            job_type=job_type,
            state=COIDetectionJobStates.PENDING,
        )

        # Run detection as background Celery task
        tasks.run_coi_detection.delay(str(job.uuid))

        return response.Response(
            serializers.COIDetectionJobSerializer(
                job, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )

    detect_conflicts_serializer_class = serializers.TriggerCOIDetectionSerializer
    detect_conflicts_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description=(
            "Get or update the call's assignment settings: the response "
            "window of review assignments, reminders and reassignment. A call "
            "without settings reads the defaults."
        ),
        responses=serializers.CallAssignmentConfigurationSerializer,
    )
    @decorators.action(
        detail=True, methods=["get", "patch"], url_path="assignment-configuration"
    )
    def assignment_configuration(self, request, uuid=None):
        """Get or update a call's assignment settings, as its matching
        configuration is: from the call, created on first use."""
        call = self.get_object()
        config, _ = models.CallAssignmentConfiguration.objects.get_or_create(call=call)
        if request.method == "GET":
            serializer = serializers.CallAssignmentConfigurationSerializer(
                config, context=self.get_serializer_context()
            )
            return response.Response(serializer.data)
        serializer = serializers.CallAssignmentConfigurationSerializer(
            config,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data)

    assignment_configuration_serializer_class = (
        serializers.CallAssignmentConfigurationSerializer
    )
    assignment_configuration_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    # =========================================================================
    # Matching
    # =========================================================================

    @extend_schema(
        description="Get or update matching configuration for this call.",
        responses=serializers.MatchingConfigurationSerializer,
    )
    @decorators.action(
        detail=True, methods=["get", "patch"], url_path="matching-configuration"
    )
    def matching_configuration(self, request, uuid=None):
        """Get or update matching configuration for a call."""
        call = self.get_object()

        config, created = models.MatchingConfiguration.objects.get_or_create(call=call)

        if request.method == "GET":
            serializer = serializers.MatchingConfigurationSerializer(
                config, context=self.get_serializer_context()
            )
            return response.Response(serializer.data)

        # PATCH - update configuration
        serializer = serializers.MatchingConfigurationSerializer(
            config,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data)

    matching_configuration_serializer_class = (
        serializers.MatchingConfigurationSerializer
    )
    matching_configuration_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        request=None,
        description="Compute affinity scores for all reviewer-proposal pairs.",
        responses={200: serializers.ComputeAffinitiesResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="compute-affinities")
    def compute_affinities(self, request, uuid=None):
        """Compute affinity scores for reviewer-proposal matching."""
        reviewer_standing.ensure_automated_matching_enabled()
        call = self.get_object()

        affinities = affinity_scoring.compute_affinities_for_call(call)

        return response.Response(
            {
                "computed_count": len(affinities),
                "message": f"Computed {len(affinities)} affinity scores.",
            }
        )

    compute_affinities_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Get affinity matrix for reviewer-proposal matching.",
        responses={200: serializers.AffinityMatrixResponseSerializer},
        parameters=[
            OpenApiParameter(
                "scope",
                str,
                OpenApiParameter.QUERY,
                description="Filter by reviewer source: 'pool' (accepted reviewers), 'suggestions' (suggested reviewers), or 'all' (both). Default: 'pool'",
                enum=AffinityMatrixScopes.VALUES,
            ),
        ],
    )
    @decorators.action(detail=True, methods=["get"], url_path="affinity-matrix")
    def affinity_matrix(self, request, uuid=None):
        """Get affinity matrix for this call."""
        reviewer_standing.ensure_automated_matching_enabled()
        call = self.get_object()

        scope = request.query_params.get("scope", AffinityMatrixScopes.POOL)
        if scope not in AffinityMatrixScopes.VALUES:
            scope = AffinityMatrixScopes.POOL

        matrix = affinity_scoring.get_affinity_matrix(call, scope=scope)
        return response.Response(matrix)

    affinity_matrix_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        description="Get proposed reviewer-proposal assignments.",
        responses=serializers.ProposedAssignmentSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="proposed-assignments")
    def proposed_assignments(self, request, uuid=None):
        """Get proposed assignments for this call."""
        call = self.get_object()

        assignments = models.ProposedAssignment.objects.filter(call=call).order_by(
            "proposal", "-affinity_score"
        )

        # Filter by deployment status
        is_deployed = request.query_params.get("is_deployed")
        if is_deployed is not None:
            assignments = assignments.filter(is_deployed=is_deployed.lower() == "true")

        serializer = serializers.ProposedAssignmentSerializer(
            assignments,
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data)

    proposed_assignments_serializer_class = serializers.ProposedAssignmentSerializer
    proposed_assignments_permissions = [
        proposal_permissions.support_can_read(
            permission_factory(
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                ["*", "manager"],
            )
        )
    ]

    @extend_schema(
        description="Generate assignment batches for reviewers. "
        "Uses the affinity matrix and COI records to assign reviewers to proposals.",
        request=serializers.GenerateAssignmentsSerializer,
        responses={200: serializers.GenerateAssignmentsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="generate-assignments")
    @transaction.atomic
    def generate_assignments(self, request, uuid=None):
        """
        Generate assignment batches for reviewers.

        This creates draft AssignmentBatch records with AssignmentItems for each
        reviewer-proposal pair. The call manager can then review and send them.
        """
        reviewer_standing.ensure_automated_matching_enabled()
        call = self.get_object()

        serializer = serializers.GenerateAssignmentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        proposal_uuids = serializer.validated_data.get("proposal_uuids", [])
        reviewers_per_proposal = serializer.validated_data.get("reviewers_per_proposal")

        # The call's matching configuration sets how many reviewers a
        # proposal gets and how many proposals a reviewer takes; without one,
        # the configuration's own defaults.
        matching_config = models.MatchingConfiguration.objects.filter(
            call=call
        ).first() or models.MatchingConfiguration(call=call)
        if not reviewers_per_proposal:
            reviewers_per_proposal = matching_config.min_reviewers_per_proposal
        elif (
            matching_config.pk
            and reviewers_per_proposal > matching_config.max_reviewers_per_proposal
        ):
            raise exceptions.ValidationError(
                {
                    "reviewers_per_proposal": _(
                        "The call allows at most %(max)s reviewers per proposal."
                    )
                    % {"max": matching_config.max_reviewers_per_proposal}
                }
            )
        # Applied only when the call configures it, so the pool entry's own
        # limit is all that holds otherwise.
        max_proposals_per_reviewer = (
            matching_config.max_proposals_per_reviewer if matching_config.pk else None
        )
        # A reviewer scored below this for a proposal is not offered it.
        min_affinity = matching_config.min_affinity_threshold

        # Get proposals needing assignments
        if proposal_uuids:
            proposals = models.Proposal.objects.filter(
                uuid__in=proposal_uuids,
                round__call=call,
                state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW],
            )
        else:
            proposals = models.Proposal.objects.filter(
                round__call=call,
                state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW],
            )

        # Get accepted pool members
        # Accepted pool members who may still be given work
        pool_entries = models.CallReviewerPool.objects.eligible().filter(
            call=call,
            reviewer__isnull=False,  # Must have a profile
        )

        if not pool_entries.exists():
            return response.Response(
                {
                    "batches_created": 0,
                    "items_created": 0,
                    "proposals_processed": 0,
                    "skipped_proposals": [
                        {
                            "reason": _("No accepted reviewers in pool"),
                        }
                    ],
                },
                status=status.HTTP_200_OK,
            )

        batches_created = 0
        items_created = 0
        proposals_processed = 0
        skipped_proposals = []

        # Convert to list for multiple iterations
        proposals_list = list(proposals)
        proposal_ids = [p.id for p in proposals_list]

        # Prefetch all blocking COIs for all proposals at once
        # Build lookup: proposal_id -> set of reviewer_ids with blocking COI
        blocking_cois = models.ConflictOfInterest.objects.filter(
            proposal_id__in=proposal_ids,
            status__in=["pending", "recused"],
        ).values_list("proposal_id", "reviewer_id")
        blocking_coi_by_proposal = {}
        for proposal_id, reviewer_id in blocking_cois:
            blocking_coi_by_proposal.setdefault(proposal_id, set()).add(reviewer_id)

        # Prefetch all existing assignments for all proposals at once
        # Build lookup: proposal_id -> set of pool_entry_ids already assigned
        existing_items = models.AssignmentItem.objects.filter(
            proposal_id__in=proposal_ids,
        ).values_list("proposal_id", "batch__reviewer_pool_entry_id")
        existing_by_proposal = {}
        for proposal_id, pool_entry_id in existing_items:
            existing_by_proposal.setdefault(proposal_id, set()).add(pool_entry_id)

        # Who already reviews each proposal, by a review they have not
        # declined: given directly, or taken on from an earlier batch. They
        # are neither offered it again nor counted short.
        reviewing_by_proposal = {}
        for proposal_id, user_id in (
            models.Review.objects.filter(proposal_id__in=proposal_ids)
            .exclude(state=models.Review.States.REJECTED)
            .values_list("proposal_id", "reviewer_id")
        ):
            reviewing_by_proposal.setdefault(proposal_id, set()).add(user_id)
        # Offers still open count towards the reviewers a proposal needs.
        offered_by_proposal = {}
        for proposal_id, user_id in models.AssignmentItem.objects.filter(
            proposal_id__in=proposal_ids,
            status__in=(
                models.AssignmentItemStatuses.PENDING,
                models.AssignmentItemStatuses.ACCEPTED,
            ),
        ).values_list("proposal_id", "batch__reviewer_pool_entry__reviewer__user_id"):
            offered_by_proposal.setdefault(proposal_id, set()).add(user_id)

        # Prefetch all affinities for all proposals at once
        # Build lookup: proposal_id -> list of (reviewer_id, affinity_score)
        affinities_qs = (
            models.ReviewerProposalAffinity.objects.filter(
                proposal_id__in=proposal_ids,
            )
            .order_by("-affinity_score")
            .values_list("proposal_id", "reviewer_id", "affinity_score")
        )
        affinities_by_proposal = {}
        for proposal_id, reviewer_id, score in affinities_qs:
            affinities_by_proposal.setdefault(proposal_id, []).append(
                (reviewer_id, score)
            )

        # Build pool entries lookup by reviewer_id for O(1) access.
        # The entries are locked first so that a concurrent assignment to the
        # same reviewers waits and then counts the items created here.
        list(pool_entries.select_for_update().values_list("pk", flat=True))
        pool_entries_list = list(
            pool_entries.select_related("reviewer").with_open_assignments()
        )
        # Kept up to date as items are created, so the limit also holds for
        # entries selected earlier in this run.
        open_assignments = {
            entry.id: entry.open_assignments for entry in pool_entries_list
        }

        def has_capacity(entry):
            limit = entry.max_assignments
            if max_proposals_per_reviewer is not None:
                limit = min(limit, max_proposals_per_reviewer)
            return open_assignments[entry.id] < limit

        pool_entries_by_reviewer = {
            entry.reviewer_id: entry for entry in pool_entries_list
        }

        # Prefetch all COI records for setting on items later
        # Build lookup: (reviewer_id, proposal_id) -> list of COI objects
        all_coi_records = models.ConflictOfInterest.objects.filter(
            proposal_id__in=proposal_ids,
            status__in=["pending", "recused"],
        )
        coi_by_reviewer_proposal = {}
        for coi in all_coi_records:
            key = (coi.reviewer_id, coi.proposal_id)
            coi_by_reviewer_proposal.setdefault(key, []).append(coi)

        # Track batches and items created during this run for deduplication
        created_batches = {}  # (pool_entry_id,) -> batch
        created_items = set()  # (batch_id, proposal_id)

        # For each proposal, select reviewers
        for proposal in proposals_list:
            proposal_id = proposal.id

            # Get blocking COI reviewer IDs from prefetched data
            blocking_reviewer_ids = blocking_coi_by_proposal.get(proposal_id, set())

            # Get existing assignment pool entry IDs from prefetched data
            existing_pool_entry_ids = existing_by_proposal.get(proposal_id, set())

            reviewing_user_ids = reviewing_by_proposal.get(proposal_id, set())
            covered = len(
                reviewing_user_ids | offered_by_proposal.get(proposal_id, set())
            )
            needed = reviewers_per_proposal - covered
            if needed <= 0:
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": _("Already has enough reviewers"),
                    }
                )
                continue

            # Filter eligible pool entries using prefetched data
            eligible_entries = [
                entry
                for entry in pool_entries_list
                if entry.reviewer_id not in blocking_reviewer_ids
                and entry.id not in existing_pool_entry_ids
                and entry.reviewer.user_id not in reviewing_user_ids
            ]

            if not eligible_entries:
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": _("No eligible reviewers available"),
                    }
                )
                continue

            eligible_reviewer_ids = {e.reviewer_id for e in eligible_entries}

            # Get affinity scores from prefetched data, filtered to eligible
            # reviewers who match the proposal closely enough
            scored_affinities = affinities_by_proposal.get(proposal_id, [])
            scored_reviewer_ids = {
                reviewer_id for reviewer_id, _score in scored_affinities
            }
            proposal_affinities = [
                (reviewer_id, score)
                for reviewer_id, score in scored_affinities
                if reviewer_id in eligible_reviewer_ids and score >= min_affinity
            ]

            # Select top reviewers by affinity: walk the affinities best
            # first, passing over reviewers without capacity, until enough
            selected_entries = []
            selected_entry_ids = set()
            for reviewer_id, affinity_score in proposal_affinities:
                if len(selected_entries) >= needed:
                    break
                entry = pool_entries_by_reviewer.get(reviewer_id)
                if entry and entry.id not in selected_entry_ids and has_capacity(entry):
                    selected_entries.append((entry, affinity_score))
                    selected_entry_ids.add(entry.id)

            # If we don't have enough from affinity, add reviewers whose
            # affinity to the proposal is not known. One scored below the
            # minimum is a known poor match and is left out.
            if len(selected_entries) < needed:
                for entry in eligible_entries:
                    if entry.id in selected_entry_ids:
                        continue
                    if entry.reviewer_id in scored_reviewer_ids:
                        continue
                    if has_capacity(entry):
                        selected_entries.append((entry, None))
                        selected_entry_ids.add(entry.id)
                        if len(selected_entries) >= needed:
                            break

            if not selected_entries:
                below_minimum = not proposal_affinities and any(
                    reviewer_id in eligible_reviewer_ids
                    for reviewer_id, _score in scored_affinities
                )
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": (
                            _("No eligible reviewer reaches the minimum affinity")
                            if below_minimum
                            else _("Could not find enough eligible reviewers")
                        ),
                    }
                )
                continue

            # Create assignment items for each selected reviewer
            for entry, affinity_score in selected_entries:
                # Find or create draft batch for this reviewer
                batch_key = entry.id
                if batch_key in created_batches:
                    batch = created_batches[batch_key]
                    created = False
                else:
                    batch, created = models.AssignmentBatch.objects.get_or_create(
                        call=call,
                        reviewer_pool_entry=entry,
                        status=models.AssignmentBatchStatuses.DRAFT,
                        defaults={
                            "source": models.AssignmentSources.ALGORITHM,
                            "created_by": request.user,
                        },
                    )
                    created_batches[batch_key] = batch
                if created:
                    batches_created += 1

                # Check for existing item using in-memory tracking
                item_key = (batch.id, proposal.id)
                if item_key not in created_items:
                    # Get COI records from prefetched data
                    coi_key = (entry.reviewer_id, proposal.id)
                    coi_list = coi_by_reviewer_proposal.get(coi_key, [])
                    has_coi = len(coi_list) > 0

                    item = models.AssignmentItem.objects.create(
                        batch=batch,
                        proposal=proposal,
                        affinity_score=affinity_score,
                        has_coi=has_coi,
                        status=models.AssignmentItemStatuses.COI_BLOCKED
                        if has_coi
                        else models.AssignmentItemStatuses.PENDING,
                    )
                    if has_coi:
                        item.coi_records.set(coi_list)
                    else:
                        open_assignments[entry.id] += 1

                    created_items.add(item_key)
                    items_created += 1

            proposals_processed += 1

        return response.Response(
            {
                "batches_created": batches_created,
                "items_created": items_created,
                "proposals_processed": proposals_processed,
                "skipped_proposals": skipped_proposals,
            },
            status=status.HTTP_200_OK,
        )

    generate_assignments_serializer_class = serializers.GenerateAssignmentsSerializer
    generate_assignments_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Send all draft assignment batches for this call.",
        request=serializers.SendAllAssignmentBatchesSerializer,
        responses={200: serializers.SendAllAssignmentBatchesResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="send-all-assignments")
    def send_all_assignments(self, request, uuid=None):
        """Send all draft assignment batches for this call."""
        call = self.get_object()

        serializer = serializers.SendAllAssignmentBatchesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        batch_uuids = serializer.validated_data.get("batch_uuids", [])

        if batch_uuids:
            batches = models.AssignmentBatch.objects.filter(
                uuid__in=batch_uuids,
                call=call,
                status=models.AssignmentBatchStatuses.DRAFT,
            )
        else:
            batches = models.AssignmentBatch.objects.filter(
                call=call,
                status=models.AssignmentBatchStatuses.DRAFT,
            )

        sent_count = 0
        skipped_count = 0

        for batch in batches:
            try:
                batch.send_invitation(user=request.user)
            except Exception:
                skipped_count += 1
                continue
            sent_count += 1
            tasks.send_assignment_batch_invitation.delay(batch.uuid)

        return response.Response(
            {
                "batches_sent": sent_count,
                "skipped": skipped_count,
            },
            status=status.HTTP_200_OK,
        )

    send_all_assignments_serializer_class = (
        serializers.SendAllAssignmentBatchesSerializer
    )
    send_all_assignments_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Create a manual assignment batch for a specific reviewer. "
        "This allows call managers to manually assign proposals to reviewers.",
        request=serializers.CreateManualAssignmentSerializer,
        responses={200: serializers.CreateManualAssignmentResponseSerializer},
    )
    @decorators.action(
        detail=True, methods=["post"], url_path="create-manual-assignment"
    )
    @transaction.atomic
    def create_manual_assignment(self, request, uuid=None):
        """
        Create a manual assignment batch for a specific reviewer.
        Creates a draft batch that can be reviewed before sending.
        """
        call = self.get_object()

        serializer = serializers.CreateManualAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pool_entry_uuid = serializer.validated_data["reviewer_pool_entry_uuid"]
        proposal_uuids = list(
            dict.fromkeys(serializer.validated_data["proposal_uuids"])
        )
        manager_notes = serializer.validated_data.get("manager_notes", "")
        override_workload_limit = serializer.validated_data["override_workload_limit"]

        # Get the reviewer pool entry, locked so that concurrent assignments
        # to the same reviewer are checked against the limit one at a time.
        try:
            pool_entry = (
                models.CallReviewerPool.objects.eligible()
                .select_for_update(of=("self",))
                .get(uuid=pool_entry_uuid, call=call)
            )
        except models.CallReviewerPool.DoesNotExist:
            raise exceptions.ValidationError(
                {
                    "reviewer_pool_entry_uuid": _(
                        "Reviewer not found in pool or not accepted."
                    )
                }
            )

        # Get proposals
        proposals = models.Proposal.objects.filter(
            uuid__in=proposal_uuids,
            round__call=call,
        )

        if not proposals.exists():
            raise exceptions.ValidationError(
                {"proposal_uuids": _("No valid proposals found.")}
            )

        out_of_evaluation = sorted(
            proposal.name
            for proposal in proposals
            if proposal.state not in ProposalStates.UNDER_EVALUATION
        )
        if out_of_evaluation:
            raise exceptions.ValidationError(
                {
                    "proposal_uuids": _(
                        "These proposals are not under evaluation and cannot "
                        "be assigned to reviewers: %(names)s."
                    )
                    % {"names": ", ".join(out_of_evaluation)}
                }
            )

        skipped_proposals = []
        new_items = []

        for proposal in proposals:
            # Already assigned: a live item in any batch, or any item at all in
            # the draft batch the new items would join -- a conflict-blocked one
            # included, which a second insert would duplicate.
            if models.AssignmentItem.objects.filter(
                Q(
                    status__in=[
                        models.AssignmentItemStatuses.PENDING,
                        models.AssignmentItemStatuses.ACCEPTED,
                        models.AssignmentItemStatuses.COI_BLOCKED,
                    ]
                )
                | Q(batch__status=models.AssignmentBatchStatuses.DRAFT),
                batch__reviewer_pool_entry=pool_entry,
                proposal=proposal,
            ).exists():
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": _("Assignment already exists for this reviewer"),
                    }
                )
                continue

            # Check for blocking COI
            coi_records = list(
                models.ConflictOfInterest.objects.filter(
                    reviewer=pool_entry.reviewer,
                    proposal=proposal,
                    status__in=["pending", "recused"],
                )
            )
            new_items.append((proposal, coi_records))

        # Nothing qualifies: no batch is made, and the caller is told why
        # each proposal was skipped instead of receiving an empty draft.
        if not new_items:
            raise exceptions.ValidationError(
                {
                    "proposal_uuids": [
                        _("Nothing to assign: %(name)s: %(reason)s")
                        % {"name": item["proposal_name"], "reason": item["reason"]}
                        for item in skipped_proposals
                    ],
                    "skipped_proposals": skipped_proposals,
                }
            )

        # COI-blocked items do not add to the reviewer's workload.
        check_reviewer_workload_limit(
            pool_entry,
            sum(1 for _proposal, coi_records in new_items if not coi_records),
            override_workload_limit,
            request.user,
        )

        # Create or get existing draft batch for this reviewer
        batch, batch_created = models.AssignmentBatch.objects.get_or_create(
            call=call,
            reviewer_pool_entry=pool_entry,
            status=models.AssignmentBatchStatuses.DRAFT,
            defaults={
                "source": models.AssignmentSources.MANUAL,
                "manager_notes": manager_notes,
                "created_by": request.user,
            },
        )

        # If batch already existed, update notes if provided
        if not batch_created and manager_notes:
            batch.manager_notes = manager_notes
            batch.save(update_fields=["manager_notes"])

        for proposal, coi_records in new_items:
            item = models.AssignmentItem.objects.create(
                batch=batch,
                proposal=proposal,
                affinity_score=None,  # Manual assignment - no affinity
                has_coi=bool(coi_records),
                status=models.AssignmentItemStatuses.COI_BLOCKED
                if coi_records
                else models.AssignmentItemStatuses.PENDING,
            )
            if coi_records:
                item.coi_records.set(coi_records)

        items_created = len(new_items)

        return response.Response(
            {
                "batch_uuid": str(batch.uuid),
                "items_created": items_created,
                "skipped_proposals": skipped_proposals,
            },
            status=status.HTTP_200_OK,
        )

    create_manual_assignment_serializer_class = (
        serializers.CreateManualAssignmentSerializer
    )
    create_manual_assignment_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Email the applicants of selected proposals of this call. "
        "One message per recipient per proposal, sent asynchronously; each "
        "proposal gets an event recording it.",
        request=serializers.NotifyProposalsSerializer,
        responses={200: serializers.NotifyProposalsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="notify-proposals")
    @transaction.atomic
    def notify_proposals(self, request, uuid=None):
        call = self.get_object()

        serializer = serializers.NotifyProposalsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal_uuids = serializer.validated_data["proposal_uuids"]
        recipients = serializer.validated_data["recipients"]
        subject = serializer.validated_data["subject"]
        body = serializer.validated_data["body"]

        # A mail switched off on this deployment would be dropped silently,
        # while each proposal's activity said it was sent: refuse instead.
        if not core_models.Notification.objects.filter(
            key="proposal.proposal_manager_message", enabled=True
        ).exists():
            raise exceptions.ValidationError(
                _(
                    "Mail to applicants is switched off on this site: an "
                    "administrator enables the 'proposal_manager_message' "
                    "notification first."
                )
            )

        found = list(
            models.Proposal.objects.filter(
                uuid__in=proposal_uuids, round__call=call
            ).select_related("created_by", "round__call")
        )
        found_uuids = {proposal.uuid for proposal in found}
        skipped_proposals = [
            {
                "proposal_uuid": str(proposal_uuid),
                "proposal_name": "",
                "reason": _("Proposal not found in this call."),
            }
            for proposal_uuid in proposal_uuids
            if proposal_uuid not in found_uuids
        ]
        # Only a proposal someone on it receives mail for is told it was
        # written to; the others are reported, so the manager can follow up.
        proposals = []
        for proposal in found:
            to, _cc = notification_rules.manager_message_emails(proposal, recipients)
            if to:
                proposals.append(proposal)
            else:
                skipped_proposals.append(
                    {
                        "proposal_uuid": proposal.uuid.hex,
                        "proposal_name": proposal.name,
                        "reason": _("Nobody it is addressed to receives mail."),
                    }
                )

        actor = request.user.full_name or request.user.username
        for proposal in proposals:
            # Placeholders, not an f-string: the subject is free text that may
            # hold braces.
            event_logger.emit(
                "{actor} sent a message to the {audience} of proposal "
                "{proposal_name}: {subject}",
                event_type=EventType.PROPOSAL_NOTIFICATION_SENT,
                event_context={
                    "proposal": proposal,
                    "actor": actor,
                    "audience": recipients,
                    "subject": subject,
                },
                scopes=[proposal],
            )

        if proposals:
            notified_uuids = [proposal.uuid.hex for proposal in proposals]
            transaction.on_commit(
                lambda: tasks.notify_proposals.delay(
                    notified_uuids, recipients, subject, body
                )
            )

        return response.Response(
            {
                "proposals_notified": len(proposals),
                "skipped_proposals": skipped_proposals,
            },
            status=status.HTTP_200_OK,
        )

    notify_proposals_serializer_class = serializers.NotifyProposalsSerializer

    @extend_schema(
        summary="Get call manager dashboard stats",
        description=(
            "Returns counts for the call manager dashboard: pending "
            "assessments, active calls managed by the user, and reviews on "
            "calls they manage that are due within the next few days."
        ),
        responses={200: serializers.DashboardCallManagerStatsSerializer},
    )
    @no_count_action
    @decorators.action(detail=False, methods=["get"], url_path="dashboard-stats")
    def dashboard_stats(self, request):
        user = request.user
        managed_call_ids = get_connected_calls(user, CallRole.MANAGER)

        active_calls = models.Call.objects.filter(
            id__in=managed_call_ids, state=CallStates.ACTIVE
        ).count()
        pending_assessments = models.Proposal.objects.filter(
            round__call_id__in=managed_call_ids,
            state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW],
        ).count()
        due_within_days = config.PROPOSAL_DASHBOARD_REVIEWS_DUE_WITHIN_DAYS
        reviews_due_soon = (
            models.Review.objects.filter(proposal__round__call_id__in=managed_call_ids)
            .due_within(due_within_days)
            .count()
        )
        return response.Response(
            {
                "pending_assessments": pending_assessments,
                "active_calls": active_calls,
                "reviews_due_soon": reviews_due_soon,
                "reviews_due_within_days": due_within_days,
            },
            status=status.HTTP_200_OK,
        )


def _call_stats(call):
    """Dashboard counts per round and for the whole call (the rounds' sum).

    Grouped aggregates only, so the query count does not depend on the number
    of rounds or proposals.
    """
    Statuses = proposal_enums.WorkflowStepInstanceStatuses
    rounds = list(call.round_set.order_by("start_time", "id"))
    counts = {r.id: Counter() for r in rounds}
    proposals = models.Proposal.objects.filter(round__call=call)
    reviews = models.Review.objects.filter(proposal__round__call=call)

    # A proposal's current step is the key of its active step instance.
    for row in proposals.values("round_id", "state", "current_step__step").annotate(
        n=Count("id")
    ):
        counts[row["round_id"]]["proposals", row["state"]] += row["n"]
        counts[row["round_id"]]["steps", row["current_step__step"]] += row["n"]

    # The call's eligibility decisions stand for the administrative check:
    # its step key is the call's own.
    admin_checks = models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round__call=call,
        call_step__outcome_set=proposal_enums.OutcomeSets.ELIGIBILITY,
    )
    for row in admin_checks.values("proposal__round_id").annotate(
        approved=Count(
            "id",
            filter=Q(
                status=Statuses.COMPLETED,
                outcome=proposal_enums.WorkflowStepOutcomes.ELIGIBLE,
            ),
        ),
        missing=Count("id", filter=Q(status__in=[Statuses.ACTIVE, Statuses.EXPIRED])),
    ):
        counts[row["proposal__round_id"]]["admin_approved"] += row["approved"]
        counts[row["proposal__round_id"]]["missing_admin_checks"] += row["missing"]

    for row in reviews.values("proposal__round_id", "state").annotate(n=Count("id")):
        counts[row["proposal__round_id"]]["reviews", row["state"]] += row["n"]

    overdue = reviews.with_deadline().filter(
        state=models.Review.States.IN_REVIEW, review_deadline__lt=timezone.now()
    )
    for round_id in overdue.values_list("proposal__round_id", flat=True):
        counts[round_id]["overdue_reviews"] += 1

    steps = list(
        call.workflow_steps.filter(is_enabled=True)
        .order_by("order", "id")
        .only("key", "name", "contributor_source", "min_contributions")
    )
    # A step of reviews from assigned reviewers is short while a proposal
    # standing at it has fewer submitted reviews than the step needs.
    short_by_key = {
        step.key: step.min_contributions
        for step in steps
        if step.contributor_source_kind
        == proposal_enums.ContributorSources.ASSIGNED_REVIEWERS
        and step.min_contributions
    }
    if short_by_key:
        short = (
            proposals.filter(current_step__step__in=short_by_key)
            .annotate(
                submitted=Count(
                    "review", filter=Q(review__state=models.Review.States.SUBMITTED)
                )
            )
            .values_list("round_id", "current_step__step", "submitted")
        )
        for round_id, key, submitted in short:
            if submitted < short_by_key[key]:
                counts[round_id]["missing_reviews"] += 1

    def totalled(c, kind, choices):
        by_state = {state: c[kind, state] for state, _label in choices}
        return {"total": sum(by_state.values()), **by_state}

    def block(c):
        return {
            "proposals": totalled(c, "proposals", ProposalStates.CHOICES),
            "reviews": totalled(c, "reviews", models.Review.States.CHOICES),
            "admin_approved": c["admin_approved"],
            "missing_admin_checks": c["missing_admin_checks"],
            "missing_reviews": c["missing_reviews"],
            "overdue_reviews": c["overdue_reviews"],
            "steps": [
                {"step": step.key, "label": step.name, "count": c["steps", step.key]}
                for step in steps
            ],
        }

    return {
        "call": block(sum(counts.values(), Counter())),
        "rounds": [
            {
                "uuid": r.uuid,
                "slug": r.slug,
                "start_time": r.start_time,
                "cutoff_time": r.cutoff_time,
                **block(counts[r.id]),
            }
            for r in rounds
        ],
    }


def _terminal_workflow_detail(proposal_state):
    """Human-readable detail for a workflow that reached its terminal step."""
    return {
        ProposalStates.ACCEPTED: "Workflow completed. Proposal accepted.",
        ProposalStates.REJECTED: "Workflow completed. Proposal rejected.",
        ProposalStates.CANCELED: "Workflow completed. Proposal canceled.",
    }.get(proposal_state, "Workflow completed.")


# The call team's view of one proposal (ProposalViewSet._call_team_proposal).
CALL_TEAM_VIEW = access.custom(
    "Users with the staff or support flag, and holders of UPDATE_CALL on the "
    "proposal's call, its managing organisation or the customer running it who "
    "are not on the proposal's applicant team; 404 for anyone else."
)


class ProposalViewSet(
    UserChecklistMixin,
    ReviewerChecklistMixin,
    UserRoleMixin,
    ActionsViewSet,
    ActionMethodMixin,
):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalSerializer
    filterset_class = filters.ProposalFilter
    disabled_actions = ["update", "partial_update", "checklist_template"]
    model = models.Proposal

    # Who may call the routes whose checks run in the action body or in the
    # services it calls.
    access_declarations = {
        "create": [
            access.custom(
                "Any authenticated user drafts a proposal in an open round of an "
                "active call, if they match the call's user restrictions (email, "
                "affiliation, identity source, AAI); they become its manager."
            )
        ],
        "dashboard_stats": [
            access.owned("Counts of the proposals the caller created.")
        ],
        "workflow_states": [
            access.custom(
                "Whoever sees the proposal; the steps of its post-award runs only "
                "for those who read its runs (user_can_view_runs)."
            )
        ],
        "workflow_timeline": [CALL_TEAM_VIEW],
        "workflow_mails": [CALL_TEAM_VIEW],
        "preview_roles": [CALL_TEAM_VIEW],
        "preview_as": [CALL_TEAM_VIEW],
        "step_inputs": [
            access.custom(
                "Never from the applicant team, users with the staff or support "
                "flag and those overseeing the call (its managers, organisers of "
                "its managing organisation, UPDATE_CALL holders) read every "
                "step's inputs; once the step has started, whoever holds its work "
                "-- its contributors and completer, or the applicant team on an "
                "applicant step (step_inputs.can_read_inputs)."
            )
        ],
        "release_step_report": [
            access.custom(
                "Users with the staff flag; otherwise, never from the applicant "
                "team, those overseeing the call (its managers, organisers of its "
                "managing organisation) and the completer of a step the applicant "
                "does not complete (step_inputs.can_release)."
            )
        ],
        "released_reports": [
            access.custom(
                "Whoever sees the proposal; it lists only the reports they read: "
                "the call team every one, the applicant team and the call's "
                "reviewers and panel members unless the decision is held for the "
                "round's publication."
            )
        ],
        "request_changes": [
            access.custom(
                "Users with the staff flag, and, never from the applicant team, "
                "whoever acts on the step named: the call's managers and "
                "organisers driving its workflow, and the step's completer "
                "(user_can_request_changes)."
            )
        ],
        "cancel_change_request": [
            access.custom(
                "Of those who read the request: who made it, unless on the "
                "applicant team; users with the staff flag; and the call's "
                "managers and organisers driving its workflow, unless on the "
                "applicant team (user_oversees_contributions)."
            )
        ],
        "change_requests": [
            access.custom(
                "Whoever sees the proposal; it lists only the requests they read "
                "(user_reads_change_request): every one for users with the staff "
                "or support flag and the call's workflow managers, a request to "
                "the applicant for the applicant team and the step's completers, "
                "a reopened contribution for its holders and the person it is "
                "handed to."
            )
        ],
        "unlockable_parts": [
            access.custom(
                "Whoever sees the proposal reads which of its parts a request "
                "for changes may unlock."
            )
        ],
        "reopen_contribution": [
            access.custom(
                "Users with the staff flag, and the call's managers and "
                "organisers driving its workflow who are not on the applicant "
                "team (user_oversees_contributions)."
            )
        ],
        "contributions": [
            access.custom(
                "Whoever sees the proposal; it lists only the contributions they "
                "read (contributions.Access.can_read) and the slots handed to "
                "them."
            )
        ],
        "submit_contribution": [
            access.custom(
                "Whoever may fill the contribution (contributions.Access.can_fill): "
                "never from the applicant team, users with the staff flag and the "
                "call's managers and organisers; otherwise its filler -- the "
                "offering's team or the contributor holding the slot, or the "
                "person it is handed to once accepted -- while it is open on an "
                "active step."
            )
        ],
        "decline_contribution": [
            access.custom(
                "Of those who read the contribution or to whom it is handed: the "
                "person it is handed to hands it back; users with the staff flag "
                "and the call's managers and organisers, not on the applicant "
                "team, withdraw any open one; a contributor declines their own "
                "per-person slot."
            )
        ],
        "delegate_contribution": [
            access.custom(
                "Of those who read the contribution: users with the staff flag "
                "and the call's managers and organisers, not on the applicant "
                "team, and its holders (the offering's team, the person whose "
                "slot it is), on an open slot of an active step that can be "
                "handed on (contributions.may_delegate)."
            )
        ],
        "accept_contribution": [
            access.custom(
                "Only the person the contribution is handed to, while it awaits "
                "their acceptance on an active step; they reach the proposal "
                "before it is otherwise visible to them."
            )
        ],
        "offering_assessments": [
            access.custom(
                "Whoever sees the proposal; it lists every per-offering outcome "
                "for those overseeing the call who are not on the applicant team, "
                "and otherwise only the contributions the caller reads "
                "(contributions.Access.can_read)."
            )
        ],
    }
    # Read actions that look the proposal up with get_object() as retrieve
    # does, and narrow what they return further as their declarations say.
    access_filtered_actions = (
        "workflow_states",
        "released_reports",
        "change_requests",
        "unlockable_parts",
        "contributions",
        "offering_assessments",
    )

    @access.shows(
        "Users with the staff or support flag, or a site-wide LIST_PROPOSALS "
        "grant, see every proposal. Others see the proposals of calls whose "
        "organisation's customer they hold LIST_PROPOSALS on, their applicant "
        "team's, those they hold a role on, those of calls they manage or "
        "organise, a panel member's submitted ones without a blocking conflict "
        "of interest, those they review, are assigned, contribute to or hold an "
        "evaluator role on, a results reader's once the round is published, and "
        "an offering manager's or slot delegate's for their offering's part."
    )
    def get_queryset(self):
        # Annotate the inputs to workflow_service.is_awaiting_manual_advance so
        # ProposalSerializer.awaiting_manual_advance is computable without a
        # per-row query (N+1 on list). Keep these expressions in sync with
        # workflow_service.is_awaiting_manual_advance.
        return (
            filter_queryset_for_user(models.Proposal.objects.all(), self.request.user)
            .annotate(
                _awaiting_manual_step=ExpressionWrapper(
                    Q(current_step__call_step__transition_mode=TransitionModes.MANUAL),
                    output_field=BooleanField(),
                ),
                _latest_step_status=F("current_step__status"),
                # Read by can_submit.
                **models.can_submit_annotations(),
                # Read by post_award_status and the open run fields.
                _open_run_phase=Subquery(
                    models.ProposalRun.objects.filter(
                        proposal=OuterRef("pk"), state=ProposalRunStates.OPEN
                    ).values("phase")[:1]
                ),
                _open_run_uuid=Subquery(
                    models.ProposalRun.objects.filter(
                        proposal=OuterRef("pk"), state=ProposalRunStates.OPEN
                    ).values("uuid")[:1]
                ),
                _current_step_kind=F("current_step__call_step__kind"),
                _has_extension_granted=Exists(
                    models.ProposalRun.objects.filter(
                        proposal=OuterRef("pk"),
                        phase=WorkflowPhases.EXTENSION,
                        state=ProposalRunStates.APPROVED,
                    )
                ),
            )
            # ProposalSerializer.can_submit reads every requested resource, its
            # call entry and the offering's components. Prefetched here for the
            # same reason as the annotations above: without it a list pays three
            # queries per proposal. Proposal.offerings_missing_* filter these in
            # Python so the prefetch is actually used.
            .prefetch_related(
                "requestedresource_set__requested_offering__offering__components",
                # Read by open_change_requests.
                Prefetch(
                    "change_requests",
                    queryset=change_requests.open_requests()
                    .filter(
                        contribution__isnull=True,
                        step_instance__status=WorkflowStepInstanceStatuses.ACTIVE,
                    )
                    .select_related(
                        "step_instance__call_step",
                        "step_instance__run",
                        "requested_by",
                    )
                    .order_by("created", "id"),
                    to_attr="_open_change_requests",
                ),
                # Read by status_labels: only steps of calls configuring labels.
                Prefetch(
                    "workflow_step_instances",
                    queryset=models.ProposalWorkflowStepInstance.objects.filter(
                        status__in=(
                            WorkflowStepInstanceStatuses.ACTIVE,
                            WorkflowStepInstanceStatuses.COMPLETED,
                        )
                    )
                    .exclude(call_step__status_labels={})
                    .select_related("call_step")
                    .order_by("order", "id"),
                    to_attr="_labelled_steps",
                ),
                # Read by the template rule in can_submit.
                "requestedresource_set__call_resource_template__requested_offering__offering",
            )
            # ProposalSerializer.workflow_step reads the current step's key
            # and panel_name its panel; can_submit reads the call's state.
            .select_related("current_step", "panel", "round__call")
            .order_by("created")
        )

    def can_manage_role_by_oversight(self, scope, role, request):
        # Those overseeing the call manage the proposal team as an
        # organisation owner manages a project team, without a role on it.
        # The proposal's state and the roles only its managers may hand out
        # are decided by guard_proposal_team_change.
        return proposal_permissions.oversees_proposal_call(request, scope)

    def can_view_scope_team(self, user, proposal):
        # Core walks the proposal's customer/project tree, which misses users
        # whose only role is directly on the call. Call managers — and the
        # call's panel members — may view the proposal team read-only, so the
        # review interface renders (rather than crashing its team section on
        # a 403) and evaluators can comment on it.
        # The team reads itself; its author only while still on it.
        if proposal_roles.is_on_applicant_team(user, proposal):
            return True
        if super().can_view_scope_team(user, proposal):
            return True
        call_id = proposal.round.call_id
        if any(
            call_id in get_connected_calls(user, role)
            for role in (CallRole.MANAGER, CallRole.PANEL_MEMBER)
        ):
            return True
        # A reviewer -- with or without the call's reviewer role, which is the
        # pool and grants nothing by itself -- evaluates the proposals they
        # hold work on, as does the holder of an evaluator role on it.
        return holds_reviewer_work(user, proposal)

    def _concealed_team_attributes(self, scope, request):
        # The view instance lives for one request: resolve the viewer's
        # concealed attributes once for both list_users hooks.
        if not hasattr(self, "_concealed_team_attributes_cache"):
            self._concealed_team_attributes_cache = (
                serializers.get_concealed_applicant_attributes(request.user, scope)
            )
        return self._concealed_team_attributes_cache

    def validate_user_roles_query(self, scope, request):
        # A filter, search or ordering on a concealed attribute would reveal it
        # through which rows come back, so refuse it outright.
        concealed = self._concealed_team_attributes(scope, request)
        if not concealed:
            return
        refused = serializers.get_concealed_team_member_query(
            request.query_params, concealed
        )
        if refused:
            raise exceptions.ValidationError(
                {
                    param: _(
                        "The call does not expose this applicant attribute to reviewers."
                    )
                    for param in refused
                }
            )

    def filter_user_roles_representation(self, data, scope, request):
        concealed = self._concealed_team_attributes(scope, request)
        if concealed is None:
            return data
        # Reviewer-only viewer. Role expiration is team-admin metadata
        # irrelevant to evaluation; identity follows the call's applicant
        # visibility config, exactly as on the proposal itself.
        for item in data:
            item.pop("expiration_time", None)
        return serializers.filter_team_member_fields(data, concealed)

    def filter_checklist_response(self, data, obj, request):
        # Each answer names its author, a member of the applicant team: an
        # evaluator sees them only as far as the call's config exposes them.
        concealed = serializers.get_concealed_applicant_attributes(request.user, obj)
        if concealed:
            serializers.filter_checklist_answer_fields(data["questions"], concealed)
        return data

    # Both mixins use the default implementation (obj.checklist_completion)
    # UserChecklistMixin permissions - for proposal managers only
    # Checklist viewing: same permission as viewing proposal
    @access.describes(
        access.custom(
            "Staff and support; the applicant team and whoever may update the "
            "proposal; LIST_PROPOSALS holders on its call or the call's managing "
            "organisation; the call's managers; a reviewer holding a live review "
            "of it; and holders of an evaluator role on it "
            "(reads_proposal_checklist)."
        ),
        needs_object=True,
    )
    def _checklist_view_permission(request, view, obj=None):
        if not obj:
            return
        # One rule for the answers and the files attached to them
        # (Proposal.user_can_read_checklist).
        if proposal_permissions.reads_proposal_checklist(request, obj):
            return
        raise exceptions.PermissionDenied(
            "You do not have permission to view proposal checklist."
        )

    # Support reads every call, and so the answers on its proposals, and the
    # completion status those answers produce.
    checklist_permissions = [
        proposal_permissions.support_can_read(_checklist_view_permission)
    ]
    completion_status_permissions = [
        proposal_permissions.support_can_read(_checklist_view_permission)
    ]
    # Only those who may edit the proposal (managers, administrators) answer
    # it, and only while it is a draft
    submit_answers_permissions = [proposal_permissions.can_answer_proposal_checklist]

    # ReviewerChecklistMixin permissions - for proposal reviewers
    # Custom permission for compliance checklists (call managers only) or regular proposal review permissions
    _compliance_review_check = staticmethod(
        proposal_permissions.support_can_read(
            permission_factory(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)
        )
    )

    def _compliance_checklist_permission(request, view, obj=None):
        """Restrict the review view of a checklist -- the answers that trigger
        a review, the review triggers and notes -- to whoever manages the call
        for compliance checklists (a call manager, or an organiser through the
        managing organisation, as for compliance_overview; support may read),
        and to MANAGE_PROPOSAL_REVIEW holders on the call otherwise.

        Permission checks deny by raising: a returned value is ignored.
        """
        if not obj:
            return

        call = obj.round.call
        completion = view.get_checklist_completion(obj)
        if (
            completion
            and completion.checklist
            and completion.checklist.checklist_type
            == ChecklistTypes.PROPOSAL_COMPLIANCE
        ):
            # The same gate as compliance_overview, applied to the call.
            ProposalViewSet._compliance_review_check(request, view, call)
        elif not permissions_utils.has_permission(
            request, PermissionEnum.MANAGE_PROPOSAL_REVIEW, call
        ):
            raise exceptions.PermissionDenied(
                "You do not have permission to review proposal checklist."
            )

    checklist_review_permissions = [_compliance_checklist_permission]
    completion_review_status_permissions = [_compliance_checklist_permission]

    destroy_permissions = [proposal_permissions.can_manage_proposal]
    update_project_details_permissions = [proposal_permissions.can_update_proposal]

    destroy_validators = [core_validators.StateValidator(ProposalStates.DRAFT)]
    # In draft, or once submitted the fields an open request for changes
    # unlocks (ProposalUpdateProjectDetailsSerializer); never once decided.
    update_project_details_validators = [
        core_validators.StateValidator(ProposalStates.DRAFT, ProposalStates.IN_REVIEW)
    ]

    update_project_details_serializer_class = (
        serializers.ProposalUpdateProjectDetailsSerializer
    )

    @staticmethod
    def _note_change_for_applicant(request, proposal, what, changes=None):
        """Log a change to what the applicant submitted, made once it is
        submitted by someone outside the applicant team: the applicant team
        reads who changed what."""
        if proposal.state == ProposalStates.DRAFT:
            return
        if proposal_roles.is_on_applicant_team(request.user, proposal):
            return
        activity.log_edited_for_applicant(proposal, what, changes)

    @extend_schema(
        description="Update project details of a proposal.",
        request=serializers.ProposalUpdateProjectDetailsSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_project_details(self, request, uuid=None):
        proposal = self.get_object()
        with transaction.atomic():
            # Whether a field is still open is decided under the lock, so the
            # changes being submitted meanwhile are waited for.
            proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            serializer = self.get_serializer(data=request.data, instance=proposal)
            serializer.is_valid(raise_exception=True)
            before = {
                field: getattr(proposal, field)
                for field in serializer.validated_data
                if hasattr(proposal, field)
            }
            serializer.save()
            changed = {
                field: {
                    "before": str(before[field]),
                    "after": str(getattr(proposal, field)),
                }
                for field in before
                if before[field] != getattr(proposal, field)
            }
            if changed:
                self._note_change_for_applicant(
                    request, proposal, "Project details", changed
                )

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        description=(
            "Move the proposal to another panel of its call, or off any "
            "panel. For those who manage the call and the completer of the "
            "proposal's running assignment step that assigns a role held "
            "through its panel. The chair the workflow assigned from the old "
            "panel, who has done nothing on the proposal yet, is revoked "
            "with the move; any other holder of a role held through the "
            "panel refuses it: revoke them first. When the proposal's "
            "assignment step is running, the new panel's chair takes the "
            "roles held by the panel's chair. "
            "Logged on the proposal's event feed, which the applicant team "
            "does not read."
        ),
        request=serializers.ProposalSetPanelSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_panel(self, request, uuid=None):
        proposal = self.get_object()
        serializer = serializers.ProposalSetPanelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        panel = serializer.validated_data["panel"]
        settled = []
        with transaction.atomic():
            # Checked again under the proposal's lock: the assignment step
            # that lets its completer move the proposal may have completed
            # meanwhile, or their standing on it lapsed.
            locked = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            if not proposal_permissions.user_can_set_proposal_panel(
                request.user, locked
            ):
                raise exceptions.PermissionDenied(
                    "Only the call's managers and the completer of the "
                    "proposal's panel assignment step can change its panel."
                )
            proposal, previous, moved = panels.set_panel(proposal, panel, request.user)
            if previous != panel:
                activity.log_panel_changed(proposal, previous, request.user)
                # The chair the workflow gave the proposal goes with the old
                # panel, told as any holder whose role is revoked is.
                reason = f"Moved to panel {panel.name if panel else 'none'}."
                for assignment in moved:
                    assignment = proposal_roles.revoke(
                        assignment, revoked_by=request.user, reason=reason
                    )
                    contributions.sync_lead_slots(proposal, assignment.role)
                    settled.append(
                        workflow_service.settle_evaluator_slots(
                            proposal, assignment.user, request.user
                        )
                    )
                    self._notify_role_change(assignment, False)
                # A running assignment step staffs the role from the new panel.
                for instance in proposal_roles.active_instances_of_kind(
                    proposal, proposal_enums.StepKinds.ASSIGNMENT
                ):
                    workflow_service.assign_panel_chairs(instance)
        for completed, next_instance in settled:
            self._notify_contribution_decision(proposal, completed, next_instance)
        proposal = self.get_queryset().get(pk=proposal.pk)
        return response.Response(
            serializers.ProposalSerializer(
                proposal, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    set_panel_permissions = [proposal_permissions.can_set_proposal_panel]
    set_panel_serializer_class = serializers.ProposalSetPanelSerializer

    @staticmethod
    def validate_resource_requests_existing(proposal):
        if not models.RequestedResource.objects.filter(proposal=proposal).exists():
            raise exceptions.ValidationError(
                _(
                    "There must be at least some resource requests existing before moving to team validation."
                )
            )

    @extend_schema(
        description="Submit a proposal.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        proposal = self.get_object()

        # The whole block (step instances + proposal state) must be atomic —
        # a partial failure would leave orphan instances and wedge the
        # (proposal, step) unique constraint on retry. The row lock + state
        # re-check serialises concurrent submits that both passed the outer
        # StateValidator before either had committed.
        with transaction.atomic():
            locked = models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            if locked.state != ProposalStates.DRAFT:
                return response.Response(
                    {"detail": "Proposal has already been submitted."},
                    status=status.HTTP_409_CONFLICT,
                )
            proposal = locked
            previous_state = proposal.state
            # Checked again under the lock: the round may have ended since the
            # validators ran, and the draft moved on to a round not yet open,
            # or the call been archived.
            validate_call_is_active(proposal)
            validate_round_is_open(proposal)
            # A request may have been removed or added since, too.
            validate_resource_templates_used(proposal)
            # The applicant's row is locked so that two of their drafts
            # submitted at once cannot both take the last place. The draft
            # itself takes none yet, so it is not counted.
            if proposal.created_by_id is not None:
                list(
                    User.all_objects.select_for_update()
                    .filter(pk=proposal.created_by_id)
                    .values_list("pk", flat=True)
                )
            applicant_capacity.validate_applicant_capacity(
                proposal.round.call, proposal.created_by
            )

            # A proposal must have a project team before it can be submitted.
            # (The creator is normally auto-added, so a solo-PI proposal passes;
            # this enforces the invariant server-side and blocks the degenerate
            # empty-team case that the DRAFT state validator alone would allow.)
            if not permissions_utils.get_users(proposal).exists():
                return response.Response(
                    {"detail": "Proposal must have a project team before submission."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Saved here, not below: activate_first_step() saves with
            # update_fields and would drop anything set beforehand.
            proposal.submitted_at = timezone.now()
            # A choice the call no longer offers is not carried into review.
            if not proposal.round.call.allowed_duration_months:
                proposal.requested_duration_months = None
            proposal.save(update_fields=["submitted_at", "requested_duration_months"])
            activity.log_submitted(proposal)

            workflow_service.create_step_instances(proposal)
            started = None
            if proposal.round.call.evaluation_start == EvaluationStart.ON_SUBMISSION:
                started = workflow_service.activate_first_step(proposal)
            if started is None:
                # Evaluation waits for the round's cut-off, or the call has no
                # enabled step to start.
                proposal.state = ProposalStates.SUBMITTED
                proposal.save()

        tasks.notify_user_about_proposal_state_update.delay(
            proposal.uuid, previous_state, proposal.state
        )
        tasks.notify_call_managers_about_new_proposal_submission.delay(proposal.uuid)
        return response.Response(
            "Proposal has been submitted.",
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description=(
            "Withdraw a submitted proposal from evaluation, at its applicant's "
            "request. Its unfinished steps are skipped, reviews still being "
            "written are closed, unanswered review assignments lapse and the "
            "call managers are told. Not available once an award awaits the "
            "applicant's answer: declining the award is the way out then."
        ),
        request=serializers.ProposalWithdrawSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def withdraw(self, request, uuid=None):
        proposal = self.get_object()
        serializer = serializers.ProposalWithdrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")
        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            if proposal.state not in utils.WITHDRAWABLE_STATES:
                raise exceptions.ValidationError(
                    {"detail": _("Only a proposal under evaluation can be withdrawn.")}
                )
            # The award response is the applicant's step taking an award's
            # acceptance, under whatever key the call gave it.
            if proposal.workflow_step_instances.filter(
                call_step__kind=proposal_enums.StepKinds.APPLICANT_ACTION,
                call_step__outcome_set=proposal_enums.OutcomeSets.ACCEPTANCE,
                status=WorkflowStepInstanceStatuses.ACTIVE,
            ).exists():
                raise exceptions.ValidationError(
                    {
                        "detail": _(
                            "The award awaits your answer: decline it instead of "
                            "withdrawing."
                        )
                    }
                )
            utils.withdraw_proposal(proposal, request.user, reason)
            proposal_uuid = proposal.uuid.hex
            transaction.on_commit(
                lambda: tasks.notify_call_managers_about_withdrawn_proposal.delay(
                    proposal_uuid, request.user.uuid.hex, reason
                )
            )
        return response.Response(
            "Proposal has been withdrawn.", status=status.HTTP_200_OK
        )

    withdraw_permissions = [proposal_permissions.can_manage_proposal]

    submit_validators = [
        core_validators.StateValidator(ProposalStates.DRAFT),
        validate_call_is_active,
        validate_round_is_open,
        validate_project_details_complete,
        validate_resource_templates_used,
        validate_requested_amounts_present,
        validate_required_options_present,
        validate_requests_within_budgets,
        validate_resources_requested_for_offering_steps,
        validate_purchase_orders_present,
        validate_proposal_manager_present,
        validate_requested_duration,
        validate_compliance_answered,
    ]

    submit_permissions = [proposal_permissions.can_manage_proposal]

    def perform_create(self, serializer):
        # Validate user eligibility against call restrictions before creating proposal
        round_obj = serializer.validated_data.get("round")
        if round_obj:
            call = round_obj.call
            permissions_utils.validate_user_restrictions(call, self.request.user)
            # Checked again on submit, where it holds: a draft takes no place.
            applicant_capacity.validate_applicant_capacity(call, self.request.user)

        proposal: models.Proposal = serializer.save()
        activity.log_created(proposal)
        proposal.add_user(
            self.request.user,
            ProposalRole.MANAGER,
            created_by=self.request.user,
        )

    @staticmethod
    def _requested_resources_read(view, proposal, queryset):
        """The requested resources of ``proposal`` the caller reads: all of
        them, or a provider's own offerings' (``offering_scope``)."""
        scope = contributions.access_for(view.request).offering_scope(proposal)
        if scope is None:
            return queryset
        return queryset.filter(requested_offering__offering_id__in=scope)

    @staticmethod
    def _awarded_resources_read(view, proposal, queryset):
        """The items of ``proposal``'s award the caller reads: all of them,
        or a provider's own offerings' (``award_scope``)."""
        scope = contributions.access_for(view.request).award_scope(proposal)
        if scope is None:
            return queryset
        return queryset.filter(requested_offering__offering_id__in=scope)

    @extend_schema(
        methods=["get"],
        operation_id="proposal_proposals_resources_list",
        request=None,
        responses=serializers.RequestedResourceSerializer(many=True),
        description="List resources for a proposal.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_proposals_resources_set",
        request=serializers.RequestedResourceSerializer,
        responses=serializers.RequestedResourceSerializer,
        description="Create resource for a proposal.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def resources(self, request, uuid=None):
        result = self.action_list_method(
            "requestedresource_set", narrow=self._requested_resources_read
        )(self, request, uuid)
        if request.method == "POST" and result.status_code < 300:
            self._note_change_for_applicant(
                request, self.get_object(), "Requested resources"
            )
        return result

    resources_serializer_class = serializers.RequestedResourceSerializer
    resources_permissions = [proposal_permissions.can_update_proposal_attachments]

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RequestedResourceSerializer}
    )
    def resource_detail(self, request, uuid=None, obj_uuid=None):
        def validate_proposal_state(requested_resource):
            change_requests.require_applicant_may_edit(
                requested_resource.proposal, ChangeRequestParts.RESOURCES
            )

        def validate_removal(requested_resource):
            validate_proposal_state(requested_resource)
            proposal = requested_resource.proposal
            if proposal.state != ProposalStates.DRAFT and (
                change_requests.has_offering_slots(proposal)
            ):
                raise IncorrectStateException(change_requests.OFFERINGS_FIXED_MESSAGE)

        handler = self.action_detail_method(
            "requestedresource_set",
            delete_validators=[validate_removal],
            update_validators=[validate_proposal_state],
            narrow=self._requested_resources_read,
        )
        if request.method != "DELETE":
            result = handler(self, request, uuid, obj_uuid)
        else:
            # Submit checks the template rules under this lock; a removal that
            # skipped it could leave a proposal submitted without its template.
            with transaction.atomic():
                list(
                    models.Proposal.objects.select_for_update()
                    .filter(pk=self.get_object().pk)
                    .values_list("pk", flat=True)
                )
                result = handler(self, request, uuid, obj_uuid)
        if request.method not in rf_permissions.SAFE_METHODS and (
            result.status_code < 300
        ):
            self._note_change_for_applicant(
                request, self.get_object(), "Requested resources"
            )
        return result

    resource_detail_serializer_class = serializers.RequestedResourceSerializer
    resource_detail_permissions = [proposal_permissions.can_update_proposal_attachments]

    @extend_schema(
        methods=["post"],
        operation_id="proposal_proposals_resource_purchase_order_set",
        request=serializers.RequestedResourcePurchaseOrderSerializer,
        responses={
            status.HTTP_200_OK: serializers.RequestedResourcePurchaseOrderSerializer
        },
        description="Upload or replace the purchase order of a requested resource.",
    )
    @extend_schema(
        methods=["delete"],
        operation_id="proposal_proposals_resource_purchase_order_delete",
        request=None,
        responses={status.HTTP_204_NO_CONTENT: None},
        description="Remove the purchase order of a requested resource.",
    )
    def resource_purchase_order(self, request, uuid=None, obj_uuid=None):
        # get_object() applies filter_queryset_for_user, so visibility of the
        # proposal is what gates this, exactly as for resource_detail.
        proposal = cast(models.Proposal, self.get_object())
        change_requests.require_applicant_may_edit(
            proposal, ChangeRequestParts.RESOURCES
        )
        try:
            requested_resource = proposal.requestedresource_set.get(uuid=obj_uuid)
        except models.RequestedResource.DoesNotExist:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

        if request.method == "DELETE":
            if requested_resource.attachment:
                requested_resource.attachment.delete(save=False)
            requested_resource.purchase_order_reference = ""
            requested_resource.save(
                update_fields=["attachment", "purchase_order_reference"]
            )
            self._note_change_for_applicant(request, proposal, "Purchase order")
            return response.Response(status=status.HTTP_204_NO_CONTENT)

        serializer = self.get_serializer(requested_resource, data=request.data)
        serializer.is_valid(raise_exception=True)
        # Replace rather than accumulate, as the order attachment endpoint does.
        if "attachment" in serializer.validated_data and requested_resource.attachment:
            requested_resource.attachment.delete(save=False)
        serializer.save()
        self._note_change_for_applicant(request, proposal, "Purchase order")
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    resource_purchase_order_serializer_class = (
        serializers.RequestedResourcePurchaseOrderSerializer
    )
    resource_purchase_order_permissions = [
        proposal_permissions.can_update_proposal_attachments
    ]

    @staticmethod
    def _allocation_decision_is_active(proposal) -> bool:
        return proposal.workflow_step_instances.filter(
            is_allocation_decision=True,
            status=WorkflowStepInstanceStatuses.ACTIVE,
        ).exists()

    def _lock_open_award(self, proposal):
        """Lock the proposal and confirm its award may still change.

        Called inside a transaction: the decision completing concurrently must
        not provision an award that is half-way through an edit. Completion
        locks the proposal and then the step; expiry locks only the step. So
        the step is re-read under a lock of its own, in the same order, and an
        edit that gets it after the step expired or completed is refused
        instead of committing to a closed award.
        """
        models.Proposal.objects.select_for_update().get(pk=proposal.pk)
        decision = (
            proposal.workflow_step_instances.select_for_update()
            .filter(is_allocation_decision=True)
            .exclude(status=WorkflowStepInstanceStatuses.SKIPPED)
            .order_by("-created")
            .first()
        )
        if decision is None or decision.status != WorkflowStepInstanceStatuses.ACTIVE:
            raise IncorrectStateException(
                _(
                    "The awarded resources can only be changed while the "
                    "allocation decision is in progress."
                )
            )
        # A proposal that reached the step without passing through its
        # activation (seeded directly, or at the step before awards existed)
        # starts from its request rather than from nothing.
        utils.prefill_awarded_resources(proposal)

    @extend_schema(
        methods=["get"],
        operation_id="proposal_proposals_awarded_resources_list",
        request=None,
        responses=serializers.AwardedResourceSerializer(many=True),
        description=(
            "List what the allocation decision awards the proposal. Staff, "
            "support, the call's managers and organizers, and the managers of "
            "the offerings involved see it at any time; the applicant team once "
            "the decision is released."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_proposals_awarded_resources_set",
        request=serializers.AwardedResourceSerializer,
        responses=serializers.AwardedResourceSerializer,
        description=(
            "Add an item to the award. Call managers only, while the allocation "
            "decision is in progress."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def awarded_resources(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        if request.method == "POST":
            with transaction.atomic():
                self._lock_open_award(proposal)
                return self.action_list_method("awarded_resources")(self, request, uuid)
        if (
            proposal.awarded_resources_prefilled_at is None
            and self._allocation_decision_is_active(proposal)
        ):
            utils.prefill_awarded_resources(proposal)
        return self.action_list_method(
            "awarded_resources", narrow=self._awarded_resources_read
        )(self, request, uuid)

    awarded_resources_serializer_class = serializers.AwardedResourceSerializer
    awarded_resources_permissions = [proposal_permissions.can_access_awarded_resources]

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.AwardedResourceSerializer},
        description=(
            "Read, change or remove an item of the award. Changes are for call "
            "managers only, while the allocation decision is in progress."
        ),
    )
    def awarded_resource_detail(self, request, uuid=None, obj_uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        handler = self.action_detail_method(
            "awarded_resources", narrow=self._awarded_resources_read
        )
        if request.method in rf_permissions.SAFE_METHODS:
            return handler(self, request, uuid, obj_uuid)
        with transaction.atomic():
            self._lock_open_award(proposal)
            removed = (
                proposal.awarded_resources.select_related(
                    "requested_offering__offering", "requested_offering__plan", "plan"
                )
                .filter(uuid=obj_uuid)
                .first()
                if request.method == "DELETE"
                else None
            )
            result = handler(self, request, uuid, obj_uuid)
            if removed is not None and result.status_code < 300:
                activity.log_award_changed(
                    proposal,
                    "removed",
                    removed,
                    before=activity.award_item_state(removed),
                )
            return result

    awarded_resource_detail_serializer_class = serializers.AwardedResourceSerializer
    awarded_resource_detail_permissions = [
        proposal_permissions.can_access_awarded_resources
    ]

    @extend_schema(
        request=serializers.ProvisionAwardedItemSerializer,
        responses={status.HTTP_200_OK: serializers.AwardedResourceSerializer},
        description=(
            "Order an item of an allocated proposal's award again: one that "
            "allocation could not order (see its provision_error) or whose "
            "order the provider refused. Call managers and staff, once the "
            "provider's offering takes orders again."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def provision_awarded_item(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        serializer = serializers.ProvisionAwardedItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if proposal.project is None:
            return response.Response(
                {"detail": "The proposal has not been allocated yet."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            item = (
                models.AwardedResource.objects.select_for_update(of=("self",))
                .annotate(refused=models.awarded_order_refused())
                .select_related(
                    "requested_offering__offering__customer",
                    "requested_offering__plan",
                    "plan",
                    "requested_resource",
                )
                .filter(
                    proposal=proposal,
                    uuid=serializer.validated_data["awarded_resource_uuid"],
                )
                .first()
            )
            if item is None:
                raise exceptions.ValidationError(
                    {"awarded_resource_uuid": "No such item in this award."}
                )
            if item.requested_offering.state != RequestedOfferingStates.ACCEPTED:
                return response.Response(
                    {"detail": "The call no longer offers this offering."},
                    status=status.HTTP_409_CONFLICT,
                )
            if item.resource_id is not None and not item.refused:
                return response.Response(
                    {"detail": "The item is provisioned already."},
                    status=status.HTTP_409_CONFLICT,
                )
            utils.provision_awarded_item(
                item,
                proposal.project,
                order_author=utils.resolve_order_author(proposal, proposal.project),
                consumer_reviewer=request.user,
            )
        item.refresh_from_db()
        data = serializers.AwardedResourceSerializer(
            item, context=self.get_serializer_context()
        ).data
        if item.provision_error:
            return response.Response(
                {"detail": item.provision_error, **data},
                status=status.HTTP_409_CONFLICT,
            )
        return response.Response(data, status=status.HTTP_200_OK)

    provision_awarded_item_permissions = [
        proposal_permissions.can_provision_awarded_item
    ]

    @extend_schema(
        request=serializers.ProposalAwardedDurationSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalAwardedDurationSerializer},
        description=(
            "Set the project length the award grants, from the call's "
            "allowed_duration_months. Call managers only, while the allocation "
            "decision is in progress."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def set_awarded_duration(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        with transaction.atomic():
            self._lock_open_award(proposal)
            proposal.refresh_from_db()
            serializer = serializers.ProposalAwardedDurationSerializer(
                data=request.data, context={"proposal": proposal}
            )
            serializer.is_valid(raise_exception=True)
            before = proposal.awarded_duration_months
            proposal.awarded_duration_months = serializer.validated_data[
                "awarded_duration_months"
            ]
            proposal.save(update_fields=["awarded_duration_months"])
            if before != proposal.awarded_duration_months:
                activity.log_award_changed(
                    proposal,
                    f"project duration set to "
                    f"{proposal.awarded_duration_months} months",
                    None,
                    before={"duration_months": before},
                )
        return response.Response(
            {"awarded_duration_months": proposal.awarded_duration_months},
            status=status.HTTP_200_OK,
        )

    set_awarded_duration_permissions = [
        proposal_permissions.can_access_awarded_resources
    ]

    # Post-award runs: extension requests and the final report

    def _run_response(self, request, run, status_code=status.HTTP_200_OK):
        return response.Response(
            serializers.ProposalRunSerializer(
                run, context=self.get_serializer_context()
            ).data,
            status=status_code,
        )

    @extend_schema(
        request=serializers.RequestExtensionSerializer,
        responses={status.HTTP_201_CREATED: serializers.ProposalRunSerializer},
        description=(
            "Request an extension of an accepted proposal's allocation: opens "
            "an extension run on the call's extension steps. Only the "
            "proposal's team, while the proposal is not completed, has no "
            "open run, and before the end of the extension window."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def request_extension(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        serializer = serializers.RequestExtensionSerializer(
            data=request.data, context={"proposal": proposal}
        )
        serializer.is_valid(raise_exception=True)
        try:
            run = workflow_service.open_run(
                proposal,
                WorkflowPhases.EXTENSION,
                request.user,
                requested_end_date=serializer.validated_data.get("requested_end_date"),
                requested_limits=serializer.validated_data.get("requested_limits"),
            )
        except runs.RunError as e:
            return response.Response(
                {"detail": str(e)}, status=status.HTTP_409_CONFLICT
            )
        return self._run_response(request, run, status.HTTP_201_CREATED)

    request_extension_serializer_class = serializers.RequestExtensionSerializer
    request_extension_permissions = [proposal_permissions.can_request_extension]

    @extend_schema(
        request=None,
        responses={status.HTTP_201_CREATED: serializers.ProposalRunSerializer},
        description=(
            "Open an accepted proposal's final report ahead of its due date, "
            "or for a project without an end date. Call managers and staff."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def open_final_report(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        try:
            run = workflow_service.open_run(
                proposal, WorkflowPhases.CLOSING, request.user
            )
        except runs.RunError as e:
            return response.Response(
                {"detail": str(e)}, status=status.HTTP_409_CONFLICT
            )
        return self._run_response(request, run, status.HTTP_201_CREATED)

    open_final_report_permissions = [proposal_permissions.can_open_final_report]

    @extend_schema(
        request=serializers.WithdrawRunSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalRunSerializer},
        description=(
            "Call off the open extension request or final report. The "
            "proposal's team withdraws its own extension request while the "
            "request is still being written; the call's managers and staff "
            "any open run. Nothing is granted."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def withdraw_run(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        serializer = serializers.WithdrawRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Locked before the run and its active step are read: the applicant
        # calls an extension off only while its request is theirs, not once
        # it was submitted and is being assessed meanwhile.
        proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
            pk=proposal.pk
        )
        run = runs.open_run_of(proposal)
        named = serializer.validated_data.get("run_uuid")
        if run is None or (named is not None and run.uuid.hex != named.hex):
            return response.Response(
                {"detail": "The proposal has no such open run."},
                status=status.HTTP_409_CONFLICT,
            )
        user = request.user
        if not proposal_permissions.user_drives_runs(user, proposal):
            active = workflow_service.active_instances(proposal).first()
            if not (
                run.phase == WorkflowPhases.EXTENSION
                and proposal_permissions.acts_for_applicant(user, proposal)
                and active is not None
                and active.run_id == run.pk
                and active.call_step.kind == StepKinds.APPLICANT_ACTION
            ):
                raise exceptions.PermissionDenied(
                    "Only the call's managers call off this run now."
                )
        try:
            run = workflow_service.withdraw_run(proposal, run, user)
        except runs.RunError as e:
            return response.Response(
                {"detail": str(e)}, status=status.HTTP_409_CONFLICT
            )
        return self._run_response(request, run)

    withdraw_run_serializer_class = serializers.WithdrawRunSerializer
    withdraw_run_permissions = [proposal_permissions.can_view_runs]

    @extend_schema(
        responses=serializers.ProposalRunSerializer(many=True),
        description=(
            "The accepted proposal's extension requests and final report, "
            "oldest first. For whoever reads its award; the applicant team "
            "reads what was granted once it is decided."
        ),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def runs(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        queryset = proposal.runs.select_related("opened_by").order_by("created", "id")
        return response.Response(
            serializers.ProposalRunSerializer(
                queryset, many=True, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    runs_permissions = [proposal_permissions.can_view_runs]

    @extend_schema(
        request=serializers.ProposalRunUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalRunSerializer},
        description=(
            "Read an extension request or final report, or change what an "
            "open extension asks for -- the proposal's team, while its "
            "request step runs -- or grants: whoever completes its decision, "
            "while that runs."
        ),
    )
    def run_detail(self, request, uuid=None, obj_uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        run = proposal.runs.filter(uuid=obj_uuid).first()
        if run is None:
            raise Http404
        if request.method in rf_permissions.SAFE_METHODS:
            return self._run_response(request, run)
        serializer = serializers.ProposalRunUpdateSerializer(
            data=request.data, context={"proposal": proposal}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        with transaction.atomic():
            models.Proposal.objects.select_for_update(of=("self",)).get(pk=proposal.pk)
            run = models.ProposalRun.objects.select_for_update().get(pk=run.pk)
            active = (
                run.step_instances.filter(status=WorkflowStepInstanceStatuses.ACTIVE)
                .select_related("call_step", "proposal__round__call")
                .order_by("order", "id")
                .first()
            )
            if (
                not run.is_open
                or run.phase != WorkflowPhases.EXTENSION
                or active is None
            ):
                return response.Response(
                    {"detail": "The extension is not open for changes."},
                    status=status.HTTP_409_CONFLICT,
                )
            asks = {"requested_end_date", "requested_limits"} & set(data)
            grants = {"granted_end_date", "granted_limits"} & set(data)
            kind = active.call_step.kind
            if asks:
                if not proposal_permissions.acts_for_applicant(user, proposal):
                    raise exceptions.PermissionDenied(
                        "Only the proposal's team says what the extension asks for."
                    )
                if kind != StepKinds.APPLICANT_ACTION and not (
                    change_requests.run_part_unlocked(
                        run, ChangeRequestParts.RUN_REQUEST
                    )
                ):
                    return response.Response(
                        {"detail": "The request was submitted; it no longer changes."},
                        status=status.HTTP_409_CONFLICT,
                    )
            if grants:
                if proposal_roles.is_on_applicant_team(user, proposal):
                    raise exceptions.PermissionDenied(
                        "Only whoever decides the extension says what it grants."
                    )
                if kind != StepKinds.DECISION:
                    return response.Response(
                        {"detail": "What is granted is set while the decision runs."},
                        status=status.HTTP_409_CONFLICT,
                    )
                if not proposal_permissions.user_can_act_on_step(user, active):
                    raise exceptions.PermissionDenied(
                        "Only whoever decides the extension says what it grants."
                    )
            before = {field: getattr(run, field) for field in grants}
            for field, value in data.items():
                if field.endswith("_limits"):
                    value = value or {}
                setattr(run, field, value)
            update_fields = [*data, "modified"]
            if grants:
                run.granted_by = user
                update_fields.append("granted_by")
            run.save(update_fields=update_fields)
            if grants and any(before[f] != getattr(run, f) for f in grants):
                activity.log_award_changed(
                    proposal,
                    "extension grant set",
                    None,
                    before={
                        field: str(value) if value is not None else None
                        for field, value in before.items()
                    },
                )
        return self._run_response(request, run)

    run_detail_serializer_class = serializers.ProposalRunUpdateSerializer
    run_detail_permissions = [proposal_permissions.can_view_runs]

    @extend_schema(
        description="Attach document to proposal.",
        request=serializers.ProposalDocumentationSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_document(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        serializer = self.get_serializer(
            context={**self.get_serializer_context(), "proposal": proposal},
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)
        document = serializer.save(proposal=proposal, uploaded_by=request.user)

        event_logger.emit(
            "{actor_name} attached {file_name} to proposal {proposal_name}.",
            event_type=EventType.PROPOSAL_DOCUMENT_ADDED,
            event_context={
                "proposal": proposal,
                "actor_name": request.user.full_name or request.user.username,
                "file_name": _document_name(document),
            },
            scopes=[proposal],
        )
        return response.Response(status=status.HTTP_200_OK)

    attach_document_serializer_class = serializers.ProposalDocumentationSerializer
    attach_document_permissions = [proposal_permissions.can_update_proposal_attachments]

    # Workflow Step Endpoints

    @staticmethod
    def _step_instance_context(request, proposal):
        """What the caller may see of the proposal's steps, for
        ``ProposalWorkflowStepInstanceSerializer``."""
        user = request.user
        call = proposal.round.call
        # The call team -- call-wide role holders and the organisers who
        # oversee the call -- reads the call team's side of every step; a
        # call reviewer or panel member only of the steps they take part in.
        reads_as_call_team = step_inputs.reads_as_call_team(user, proposal)
        access = contributions.Access(user)
        call_team = {}

        def can_view_internal_notes(instance):
            if reads_as_call_team:
                return True
            if instance.pk not in call_team:
                call_team[instance.pk] = (
                    proposal_permissions.user_can_view_internal_notes(
                        user, proposal, instance, access
                    )
                )
            return call_team[instance.pk]

        reviewer_only = proposal_permissions.reads_as_reviewer_only(user, proposal)
        return {
            "request": request,
            "can_view_internal_notes": can_view_internal_notes,
            # A reader who is only one of the proposal's reviewers sees the
            # outcome and reason only of the steps they take part in.
            "can_view_step_outcome": lambda instance: (
                not reviewer_only
                or contributions.takes_part_in_step(user, instance, access)
            ),
            # completed_by can reveal reviewer / panel-member identity;
            # only the call team (same gate as internal notes) or calls
            # that reveal reviewer identity to submitters may see it.
            "can_view_step_actors": lambda instance: (
                can_view_internal_notes(instance)
                or call.reviewer_identity_visible_to_submitters
            ),
            # outcome / outcome_reason on the peer-review steps are review
            # content, gated by reviews_visible_to_submitters.
            "can_view_review_content": lambda instance: (
                can_view_internal_notes(instance) or call.reviews_visible_to_submitters
            ),
            # Only the call managers see a decision before the round's
            # results are published; the rest of the call team learns it
            # with the applicant.
            "decision_withheld": (
                proposal.decision_held
                and not proposal_permissions.user_can_view_held_decision(
                    request.user, proposal
                )
            ),
            # An evaluator passes the step actor gate as part of the call
            # team, but sees an applicant-side completer only as far as
            # the call's applicant visibility config exposes it.
            "concealed_applicant_attributes": (
                serializers.get_concealed_applicant_attributes(request.user, proposal)
            ),
        }

    @extend_schema(
        description="List all workflow step instances for this proposal.",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.ProposalWorkflowStepInstanceSerializer(
                many=True
            )
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def workflow_states(self, request, uuid=None):
        # Access control is the viewset-level queryset filter
        # (filter_queryset_for_user): unrelated users get an empty queryset
        # and self.get_object() returns 404 before any of this code runs.
        # Applicants, project members, and call team all reach the same
        # response shape — the per-field visibility (internal_notes) is
        # gated below via the serializer context.
        proposal = self.get_object()
        # The call step of every instance is loaded with it, so the
        # serializer's derived fields (name, applicant_visible, completer,
        # checklist status...) cost no query per row.
        instances = (
            proposal.workflow_step_instances.select_related(
                "call_step__checklist", "call_step__completer_proposal_role", "run"
            )
            .prefetch_related("input_steps")
            # The evaluation's path, then each post-award run's in turn.
            .order_by(F("run_id").asc(nulls_first=True), "order", "id")
        )
        if not proposal_permissions.user_can_view_runs(request.user, proposal):
            # Reviewers and panel members judged the proposal, not what
            # followed its award.
            instances = instances.filter(run__isnull=True)
        serializer = serializers.ProposalWorkflowStepInstanceSerializer(
            instances,
            many=True,
            context=self._step_instance_context(request, proposal),
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    workflow_states_serializer_class = (
        serializers.ProposalWorkflowStepInstanceSerializer
    )

    @extend_schema(
        summary="Timeline of the proposal's workflow",
        description=(
            "The proposal's steps, planned against actual, who completed "
            "each, and the workflow mail sent about it: the audience as a "
            "role and a count, never an address. For those who configure "
            "the proposal's call, staff and support; not for its applicant "
            "team."
        ),
        request=None,
        responses={200: serializers.WorkflowTimelineSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-timeline")
    def workflow_timeline(self, request, uuid=None):
        proposal = self._call_team_proposal(request)
        user = request.user
        return response.Response(
            serializers.WorkflowTimelineSerializer(
                workflow_observability.proposal_timeline(proposal, user)
            ).data
        )

    def _call_team_proposal(self, request):
        """The proposal, for those who configure its call, staff and
        support; 404 for anyone else."""
        proposal = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_support):
            # The call team's view of the proposal: names who completed each
            # step and what mail went out. A member of the applicant team who
            # also runs the call reads their proposal as its applicant.
            call = proposal.round.call
            if proposal_roles.is_on_applicant_team(user, proposal) or not (
                proposal_permissions.configurable_call_ids(user)
                .filter(pk=call.pk)
                .exists()
            ):
                raise Http404
        return proposal

    @extend_schema(
        summary="Workflow mail of the proposal, as sent",
        description=(
            "The mails among `uuid` (repeat it for each; from a timeline "
            "row's email_log_uuids) that the proposal's workflow sent: "
            "subject, body and addresses. For the same readers as "
            "workflow-timeline."
        ),
        parameters=[
            OpenApiParameter(
                "uuid",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                many=True,
                required=True,
            )
        ],
        request=None,
        responses={200: serializers.WorkflowMailSerializer(many=True)},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"], url_path="workflow-mails")
    def workflow_mails(self, request, uuid=None):
        proposal = self._call_team_proposal(request)
        mails = workflow_observability.proposal_email_logs(
            proposal, request.query_params.getlist("uuid")
        )
        return response.Response(
            serializers.WorkflowMailSerializer(mails, many=True).data
        )

    @extend_schema(
        summary="The roles the proposal may be previewed as",
        description=(
            "Every role of the proposal's call that its page may be previewed "
            "as -- an offering's team once per requested offering. For the "
            "same readers as workflow-timeline."
        ),
        request=None,
        responses={200: serializers.ProposalPreviewRoleSerializer(many=True)},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"], url_path="preview-roles")
    def preview_roles(self, request, uuid=None):
        proposal = self._call_team_proposal(request)
        return response.Response(
            serializers.ProposalPreviewRoleSerializer(
                dry_run.preview_roles(proposal), many=True
            ).data
        )

    @extend_schema(
        summary="Preview the proposal as a role of its call",
        description=(
            "The proposal page as a role of its call sees it now: the real "
            "read endpoints answering a synthetic person given the role, in a "
            "transaction that is rolled back. No real person is viewed as, "
            "nothing is saved and no mail is sent. For the same readers as "
            "workflow-timeline."
        ),
        request=serializers.ProposalPreviewAsSerializer,
        responses={200: serializers.DryRunViewAsResultSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["post"],
        url_path="preview-as",
        throttle_classes=[WorkflowDryRunThrottle],
    )
    def preview_as(self, request, uuid=None):
        proposal = self._call_team_proposal(request)
        params = serializers.ProposalPreviewAsSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        view_as = dict(params.validated_data)
        if not any(
            option["role"] == view_as["role"]
            and option["offering"] == view_as.get("offering")
            for option in dry_run.preview_roles(proposal)
        ):
            raise exceptions.ValidationError(
                {"role": "Not a role the proposal may be previewed as."}
            )
        origin = {
            name: request.META[name]
            for name in ("SERVER_NAME", "SERVER_PORT", "HTTP_HOST", "wsgi.url_scheme")
            if name in request.META
        }
        try:
            result = dry_run.preview(proposal, view_as, origin=origin)
        except dry_run.DryRunError as e:
            return response.Response(
                {"detail": e.message}, status=status.HTTP_400_BAD_REQUEST
            )
        except dry_run.DryRunBusy:
            return response.Response(
                {
                    "detail": "A preview or dry run of this call is in progress; try again."
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except dry_run.DryRunTimeout:
            return response.Response(
                {"detail": "The preview took too long."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception("Preview of proposal %s failed", proposal.uuid)
            return response.Response(
                {"detail": "The preview failed. Nothing was sent or saved."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return response.Response(serializers.DryRunViewAsResultSerializer(result).data)

    # Earlier steps' outputs, and reports released to the applicant

    def _step_instance_by_call_step(self, proposal, step_uuid):
        try:
            return (
                proposal.workflow_step_instances.select_related(
                    "call_step", "proposal__round__call"
                )
                .filter(call_step__uuid=step_uuid)
                # A step of an extension runs once per extension: the latest.
                .order_by("-id")
                .first()
            )
        except (ValueError, DjangoValidationError):
            return None

    @extend_schema(
        description=(
            "What the input steps of one of the proposal's workflow steps "
            "produced: their contributions with answers, reviews and outcome. "
            "For whoever holds the step's work once it has started -- its "
            "contributors and completer, or the applicant team on an "
            "applicant step, who read the public parts only -- and for those "
            "overseeing the call."
        ),
        parameters=[
            OpenApiParameter(
                name="step_uuid",
                location=OpenApiParameter.QUERY,
                required=True,
                type=OpenApiTypes.UUID,
                description="UUID of the call step whose inputs are read.",
                extensions={
                    "x-waldur-operation-id": (
                        "proposal_protected_calls_workflow_steps_list"
                    )
                },
            ),
        ],
        responses={200: serializers.StepInputSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"])
    def step_inputs(self, request, uuid=None):
        proposal = self.get_object()
        step_uuid, _key = proposal_permissions.get_step_query_params(request)
        if not step_uuid:
            raise exceptions.ValidationError(
                {"step_uuid": _("The step_uuid query parameter is required.")}
            )
        instance = self._step_instance_by_call_step(proposal, step_uuid)
        if instance is None:
            raise exceptions.ValidationError(
                {"step_uuid": _("The proposal has not reached this workflow step.")}
            )
        access = contributions.access_for(request)
        if not step_inputs.can_read_inputs(request.user, instance, access):
            raise exceptions.PermissionDenied(
                _("You do not hold the work of this step.")
            )
        step_context = self._step_instance_context(request, proposal)
        data = [
            {
                "step": serializers.ProposalWorkflowStepInstanceSerializer(
                    output.instance, context=step_context
                ).data,
                "contributions": serializers.StepContributionWithAnswersSerializer(
                    output.contributions, many=True, context={"request": request}
                ).data,
                "reviews": serializers.ProposalReviewSerializer(
                    output.reviews, many=True, context={"request": request}
                ).data,
            }
            for output in step_inputs.step_outputs(request.user, instance, access)
        ]
        return response.Response(data, status=status.HTTP_200_OK)

    step_inputs_permissions = []

    @extend_schema(
        description=(
            "Release the report of a completed assessment step configured to "
            "be released, such as a consolidated report or final feedback, to "
            "the applicant -- optionally as an edited text. By the step's "
            "completer or those overseeing the call; once per step."
        ),
        request=serializers.ReleaseStepReportSerializer,
        responses={200: serializers.ProposalWorkflowStepInstanceSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def release_step_report(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.ReleaseStepReportSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        with transaction.atomic():
            # Lock order as for the step actions: proposal, then step.
            models.Proposal.objects.select_for_update(of=("self",)).get(pk=proposal.pk)
            instance = (
                proposal.workflow_step_instances.select_for_update(of=("self",))
                .select_related("call_step", "proposal__round__call")
                .filter(call_step__uuid=data["step_uuid"])
                .first()
            )
            if instance is None:
                raise exceptions.ValidationError(
                    {"step_uuid": _("The proposal has no such workflow step.")}
                )
            if not step_inputs.can_release(request.user, instance):
                raise exceptions.PermissionDenied(
                    _("You cannot release this step's report.")
                )
            try:
                step_inputs.release(instance, request.user, data["text"])
            except step_inputs.ReleaseError as e:
                raise exceptions.ValidationError({"detail": str(e)})
        instance.refresh_from_db()
        serializer = serializers.ProposalWorkflowStepInstanceSerializer(
            instance, context=self._step_instance_context(request, proposal)
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    release_step_report_permissions = []
    release_step_report_serializer_class = serializers.ReleaseStepReportSerializer

    @extend_schema(
        description=(
            "The reports of the proposal's steps released to the applicant, "
            "such as the consolidated report or final feedback. For the call "
            "team, and for the applicant team once released -- whatever the "
            "call says about showing reviews -- unless the decision is held "
            "for the round's publication."
        ),
        responses={200: serializers.ReleasedReportSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"])
    def released_reports(self, request, uuid=None):
        proposal = self.get_object()
        user = request.user
        readable = step_inputs.can_read_released_reports(user, proposal)
        reads_as_call_team = step_inputs.reads_as_call_team(user, proposal)
        access = contributions.Access(user)
        data = []
        for instance in step_inputs.released_instances(proposal):
            # The call team's view of the report: for a call reviewer or panel
            # member only on a step they take part in.
            call_team = (
                reads_as_call_team
                or proposal_permissions.user_can_view_internal_notes(
                    user, proposal, instance, access
                )
            )
            if not (call_team or readable):
                continue
            report = step_inputs.report_contributions(instance)
            if not call_team and instance.released_text:
                # The edited text is what the applicant is meant to read.
                report = []
            data.append(
                {
                    "call_step_uuid": instance.call_step.uuid.hex,
                    "step_name": instance.call_step.name,
                    "released_at": instance.released_at,
                    "released_by_name": (
                        instance.released_by.full_name
                        if call_team and instance.released_by
                        else None
                    ),
                    "text": instance.released_text,
                    "contributions": serializers.StepContributionWithAnswersSerializer(
                        report, many=True, context={"request": request}
                    ).data,
                }
            )
        return response.Response(data, status=status.HTTP_200_OK)

    released_reports_permissions = []

    # Evaluator roles on the proposal

    def _proposal_role(self, proposal, role_uuid):
        role = models.CallProposalRole.objects.filter(
            call_id=proposal.round.call_id, uuid=role_uuid
        ).first()
        if role is None:
            raise exceptions.ValidationError(
                {"role_uuid": _("The call has no such evaluator role.")}
            )
        return role

    def _role_assignment(self, proposal, assignment_uuid):
        assignment = (
            proposal_roles.active_assignments(proposal)
            .select_related("role")
            .filter(uuid=assignment_uuid)
            .first()
        )
        if assignment is None:
            raise exceptions.ValidationError(
                {"assignment_uuid": _("No such evaluator on this proposal.")}
            )
        return assignment

    def _sees_evaluator_detail(self, proposal):
        """Whether the viewer sees evaluators' email and conflict-of-interest
        state: staff, support and those overseeing the call. Anyone else sees
        only their own."""
        user = self.request.user
        return (
            user.is_staff
            or user.is_support
            or proposal_permissions.oversees_proposal_call(user, proposal)
        )

    def _assignment_data(self, proposal, assignment):
        return serializers.ProposalRoleAssignmentSerializer(
            assignment,
            context={
                "request": self.request,
                "full_detail": self._sees_evaluator_detail(proposal),
            },
        ).data

    def _check_can_manage_role(self, proposal, role):
        if not proposal_permissions.user_can_manage_proposal_role(
            self.request.user, proposal, role
        ):
            raise exceptions.PermissionDenied(
                _("You cannot change who holds this role on the proposal.")
            )

    def _check_can_revoke(self, proposal, assignment):
        self._check_can_manage_role(proposal, assignment.role)
        if not proposal_permissions.may_revoke_holder(
            self.request.user, proposal, assignment
        ):
            raise exceptions.PermissionDenied(
                _("Only the call manager takes a role away from someone else.")
            )

    def _check_can_move_lead(self, proposal, role, to_assignment=None):
        if not proposal_permissions.may_move_lead(
            self.request.user, proposal, role, to_assignment
        ):
            raise exceptions.PermissionDenied(
                _(
                    "The role has a lead: only its lead hands it over, or "
                    "the call manager moves it."
                )
            )

    @staticmethod
    def _notify_role_change(assignment, assigned):
        if assigned:
            activity.log_evaluator_assigned(assignment)
        else:
            activity.log_evaluator_revoked(assignment)
        assignment_uuid = assignment.uuid.hex
        transaction.on_commit(
            lambda: tasks.notify_proposal_role_changed.delay(assignment_uuid, assigned)
        )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_proposals_role_assignments_list",
        request=None,
        responses=serializers.ProposalRoleAssignmentSerializer(many=True),
        parameters=[
            OpenApiParameter(
                "include_revoked",
                bool,
                OpenApiParameter.QUERY,
                description="Include revoked assignments, the role's history.",
            ),
        ],
        description=(
            "Who holds the call's evaluator roles on the proposal. For the "
            "call's managers, the holders and the completer of an assignment "
            "step; never the applicant team."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_proposals_role_assignments_set",
        request=serializers.ProposalRoleAssignSerializer,
        responses=serializers.ProposalRoleAssignmentSerializer,
        description=(
            "Assign an evaluator role on the proposal. Call managers at any "
            "time, the completer of an assignment step for the role while it "
            "runs -- who may make someone the lead only while the role has "
            "none, or when they are its lead handing it over. Refused for an "
            "ineligible user, a recorded conflict of interest, a member of "
            "the applicant team, or one holder too many."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def role_assignments(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        if request.method == "GET":
            if request.query_params.get("include_revoked") in ("true", "True", "1"):
                assignments = proposal_roles.assignment_history(proposal)
            else:
                assignments = proposal_roles.active_assignments(proposal)
            assignments = (
                proposal_roles.with_standing_conflict(assignments)
                .select_related(
                    "role",
                    "user",
                    "assigned_by",
                    "revoked_by",
                    "assigned_in__call_step",
                    "proposal__round__call",
                )
                .order_by("role__created", "role_id", "-is_lead", "created")
            )
            serializer = serializers.ProposalRoleAssignmentSerializer(
                assignments,
                many=True,
                context={
                    "request": request,
                    "full_detail": self._sees_evaluator_detail(proposal),
                },
            )
            return response.Response(serializer.data, status=status.HTTP_200_OK)

        input_serializer = serializers.ProposalRoleAssignSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        role = self._proposal_role(
            proposal, input_serializer.validated_data["role_uuid"]
        )
        self._check_can_manage_role(proposal, role)
        user = User.objects.filter(
            uuid=input_serializer.validated_data["user_uuid"], is_active=True
        ).first()
        if user is None:
            raise exceptions.ValidationError({"user_uuid": _("No such user.")})
        with transaction.atomic():
            # Checked again under the holders' lock, as a revocation is: the
            # assignment step may have completed meanwhile, or the user's own
            # standing on the proposal lapsed.
            proposal = proposal_roles.lock_role_holders(proposal, role)
            self._check_can_manage_role(proposal, role)
            if input_serializer.validated_data["is_lead"]:
                self._check_can_move_lead(proposal, role)
            assignment = proposal_roles.assign(
                proposal,
                role,
                user,
                assigned_by=request.user,
                assigned_in=proposal_roles.active_assignment_step(proposal, role),
                is_lead=input_serializer.validated_data["is_lead"],
                discloses_conflicts=self._sees_evaluator_detail(proposal),
            )
            contributions.sync_lead_slots(proposal, role)
            # Someone given back a role gets back the slots that lapsed with it.
            completed, next_instance = workflow_service.settle_evaluator_slots(
                proposal, user, request.user
            )
            self._notify_role_change(assignment, True)
        self._notify_contribution_decision(proposal, completed, next_instance)
        logger.info(
            "User %s assigned %s as %s on proposal %s",
            request.user.uuid.hex,
            user.uuid.hex,
            role.key,
            proposal.uuid.hex,
        )
        return response.Response(
            self._assignment_data(proposal, assignment),
            status=status.HTTP_201_CREATED,
        )

    role_assignments_permissions = [proposal_permissions.can_view_proposal_roles]

    # =========================================================================
    # Reviewers suggested by the proposal's evaluators
    # =========================================================================

    @extend_schema(
        methods=["get"],
        responses=serializers.ProposalReviewerSuggestionSerializer(many=True),
        description=(
            "The reviewers the caller suggested for the proposal, with where "
            "each stands."
        ),
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_proposals_my_reviewer_suggestions_create",
        request=serializers.ProposalReviewerSuggestionCreateSerializer,
        responses=serializers.ProposalReviewerSuggestionSerializer,
        description=(
            "Suggest a reviewer for the proposal: an existing user or an "
            "e-mail address, with a note. Only a holder of the evaluator role "
            "of the active step collecting reviewer suggestions, through their "
            "own contribution to it."
        ),
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def my_reviewer_suggestions(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        if request.method == "GET":
            suggestions = models.ProposalReviewerSuggestion.objects.filter(
                proposal=proposal, contribution__contributor=request.user
            ).select_related(
                "proposal",
                "suggested_user",
                "contribution__contributor",
                "contribution__proposal_step__call_step",
                "pool_entry",
            )
            return response.Response(
                serializers.ProposalReviewerSuggestionSerializer(
                    suggestions, many=True, context=self.get_serializer_context()
                ).data
            )
        input_serializer = serializers.ProposalReviewerSuggestionCreateSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        suggestion = reviewer_suggestions.suggest(
            request.user, proposal, **input_serializer.validated_data
        )
        logger.info(
            "User %s suggested a reviewer for proposal %s",
            request.user.uuid.hex,
            proposal.uuid.hex,
        )
        return response.Response(
            serializers.ProposalReviewerSuggestionSerializer(
                suggestion, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    my_reviewer_suggestions_permissions = [reviewer_suggestions.can_suggest]

    @extend_schema(
        request=serializers.ProposalReviewerSuggestionWithdrawSerializer,
        responses={204: None},
        description=(
            "Take back one's own reviewer suggestion while it is pending and "
            "the step collecting it is active."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def withdraw_reviewer_suggestion(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        input_serializer = serializers.ProposalReviewerSuggestionWithdrawSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        suggestion = (
            models.ProposalReviewerSuggestion.objects.filter(
                proposal=proposal,
                uuid=input_serializer.validated_data["suggestion_uuid"],
                contribution__contributor=request.user,
            )
            .select_related("contribution", "pool_entry")
            .first()
        )
        if suggestion is None:
            raise exceptions.ValidationError(
                {"suggestion_uuid": _("No such suggestion of yours.")}
            )
        reviewer_suggestions.withdraw(request.user, suggestion)
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    withdraw_reviewer_suggestion_permissions = [reviewer_suggestions.can_suggest]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "query",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=True,
                description=(
                    "Name, username or e-mail to search for, case-insensitive, "
                    "at least 3 characters; every word must match."
                ),
            ),
        ],
        responses=serializers.ReviewerSuggestionCandidateSerializer(many=True),
        description=(
            "Registered users the caller may suggest as reviewers: those with "
            "a published reviewer profile and those in the call's reviewer "
            "pool, at most 20, never with their e-mail address. The applicant "
            "team, the caller and the proposal's evaluator role holders are "
            "left out; a standing conflict of interest is flagged. For the "
            "holders of the evaluator role of the active step collecting "
            "reviewer suggestions."
        ),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def reviewer_suggestion_candidates(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        candidates = reviewer_suggestions.suggestion_candidates(
            request.user, proposal, request.query_params.get("query")
        )
        return response.Response(
            serializers.ReviewerSuggestionCandidateSerializer(
                candidates, many=True
            ).data
        )

    reviewer_suggestion_candidates_permissions = [
        reviewer_suggestions.can_search_candidates
    ]

    def _suggested_reviewers_data(self, candidates, reads_addresses=True):
        return serializers.SuggestedReviewerSerializer(
            candidates,
            many=True,
            context={
                **self.get_serializer_context(),
                "reads_addresses": reads_addresses,
            },
        ).data

    @extend_schema(
        responses=serializers.SuggestedReviewerSerializer(many=True),
        description=(
            "The reviewers the proposal's evaluators suggested, merged per "
            "suggested person, with who suggested them, where each stands and "
            "the conflicts of interest known. For the call's managers and the "
            "completer of a step collecting suggestions; never the applicant "
            "team. Only staff, support and those overseeing the call read a "
            "suggested user's address, and see a typed address matched to "
            "its user; the completer reads typed addresses as typed."
        ),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def suggested_reviewers(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        reads_addresses = reviewer_suggestions.reads_addresses(request.user, proposal)
        return response.Response(
            self._suggested_reviewers_data(
                reviewer_suggestions.candidates_of(
                    proposal, resolve_emails=reads_addresses
                ),
                reads_addresses=reads_addresses,
            )
        )

    suggested_reviewers_permissions = [reviewer_suggestions.can_view]

    @extend_schema(
        request=serializers.SuggestedReviewersActionSerializer,
        responses=serializers.InviteSuggestedReviewersResponseSerializer,
        description=(
            "Invite suggested reviewers to the call's reviewer pool, as an "
            "invitation by e-mail is sent: the call's response window and the "
            "invitation e-mail apply. Each suggestion of the person follows "
            "the invitation. A person already in the pool is linked to their "
            "entry instead."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def invite_suggested_reviewers(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        input_serializer = serializers.SuggestedReviewersActionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        results = reviewer_suggestions.invite(
            proposal,
            input_serializer.validated_data["suggestion_uuids"],
            request.user,
        )
        counts = {
            outcome: sum(1 for result in results if result.outcome == outcome)
            for outcome, _label in reviewer_suggestions.InvitationOutcomes.CHOICES
        }
        logger.info(
            "User %s invited %s suggested reviewer(s) of proposal %s to the pool",
            request.user.uuid.hex,
            counts[reviewer_suggestions.InvitationOutcomes.INVITED],
            proposal.uuid.hex,
        )
        return response.Response(
            serializers.InviteSuggestedReviewersResponseSerializer(
                {**counts, "results": results},
                context=self.get_serializer_context(),
            ).data
        )

    invite_suggested_reviewers_permissions = [reviewer_suggestions.can_invite]

    @extend_schema(
        request=serializers.SuggestedReviewersActionSerializer,
        responses=serializers.SuggestedReviewerSerializer(many=True),
        description="Set suggested reviewers aside, unless already invited.",
    )
    @decorators.action(detail=True, methods=["post"])
    def discard_suggested_reviewers(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        input_serializer = serializers.SuggestedReviewersActionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        candidates = reviewer_suggestions.discard(
            proposal,
            input_serializer.validated_data["suggestion_uuids"],
            request.user,
        )
        return response.Response(self._suggested_reviewers_data(candidates))

    discard_suggested_reviewers_permissions = [reviewer_suggestions.can_invite]

    @extend_schema(
        request=serializers.ProposalRoleAssignmentActionSerializer,
        responses=serializers.ProposalRoleAssignmentSerializer,
        description=(
            "Take an evaluator role away. The holder loses the access it gave; "
            "the assignment is kept as history. The completer of an "
            "assignment step takes away only their own; the call manager "
            "anyone's."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def revoke_role_assignment(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        input_serializer = serializers.ProposalRoleAssignmentActionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        assignment = self._role_assignment(
            proposal, input_serializer.validated_data["assignment_uuid"]
        )
        self._check_can_revoke(proposal, assignment)
        with transaction.atomic():
            # Checked again under the holders' lock: the assignment step may
            # have been completed, or the caller's own standing changed,
            # meanwhile.
            proposal = proposal_roles.lock_role_holders(proposal, assignment.role)
            self._check_can_revoke(proposal, assignment)
            assignment = proposal_roles.revoke(assignment, revoked_by=request.user)
            contributions.sync_lead_slots(proposal, assignment.role)
            # Their open slots go with the role; taking out the last open one
            # may be what completes the step.
            completed, next_instance = workflow_service.settle_evaluator_slots(
                proposal, assignment.user, request.user
            )
            self._notify_role_change(assignment, False)
        self._notify_contribution_decision(proposal, completed, next_instance)
        logger.info(
            "User %s revoked role assignment %s on proposal %s",
            request.user.uuid.hex,
            assignment.uuid.hex,
            proposal.uuid.hex,
        )
        return response.Response(
            self._assignment_data(proposal, assignment),
            status=status.HTTP_200_OK,
        )

    revoke_role_assignment_permissions = [proposal_permissions.can_view_proposal_roles]

    @extend_schema(
        request=serializers.ProposalRoleAssignmentActionSerializer,
        responses=serializers.ProposalRoleAssignmentSerializer,
        description=(
            "Mark a holder as the lead of their role, in place of any other. "
            "The completer of an assignment step only while the role has no "
            "lead, or as its lead handing it over; the call manager always."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def set_role_lead(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        input_serializer = serializers.ProposalRoleAssignmentActionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        assignment = self._role_assignment(
            proposal, input_serializer.validated_data["assignment_uuid"]
        )
        self._check_can_manage_role(proposal, assignment.role)
        with transaction.atomic():
            proposal_roles.lock_role_holders(proposal, assignment.role)
            self._check_can_move_lead(proposal, assignment.role, assignment)
            assignment = proposal_roles.set_lead(assignment)
            contributions.sync_lead_slots(proposal, assignment.role)
            self._notify_role_change(assignment, True)
        logger.info(
            "User %s made %s the lead %s on proposal %s",
            request.user.uuid.hex,
            assignment.user.uuid.hex,
            assignment.role.key,
            proposal.uuid.hex,
        )
        return response.Response(
            self._assignment_data(proposal, assignment),
            status=status.HTTP_200_OK,
        )

    set_role_lead_permissions = [proposal_permissions.can_view_proposal_roles]

    CANDIDATES_LIMIT = 50
    # A role anyone may hold looks people up, it does not list them.
    ANY_USER_CANDIDATES_LIMIT = 10
    CANDIDATES_MIN_QUERY = 3

    @extend_schema(
        operation_id="proposal_proposals_role_assignment_candidates_list",
        parameters=[
            OpenApiParameter(
                "role_uuid",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                required=True,
                description="The evaluator role to pick holders for.",
                extensions={
                    "x-waldur-operation-id": (
                        "proposal_protected_calls_proposal_roles_list"
                    )
                },
            ),
            OpenApiParameter(
                "query",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description=(
                    "Name, email or username to search for, case-insensitive. "
                    "Email matches in part for staff and those overseeing the "
                    "call, as a whole address for anyone else. Required for a "
                    "role any user may hold, at least 3 characters: without it "
                    "no one is listed."
                ),
            ),
        ],
        responses=serializers.ProposalRoleCandidateSerializer(many=True),
        description=(
            "Users who may hold the role on the proposal by its eligibility, "
            "at most 50 (10 for a role any user may hold), with whether they can be assigned: a conflict of "
            "interest on record or another evaluator role on the proposal "
            "rules them out. The applicant team is left out. Email, and "
            "whether a conflict is on record, are shown to staff and those "
            "overseeing the call only -- email for a role any user may hold "
            "to staff only; anyone else finds a user by email only by their "
            "whole address. For whoever may assign the role."
        ),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def role_assignment_candidates(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        role_uuid = request.query_params.get("role_uuid")
        if not role_uuid:
            raise exceptions.ValidationError(
                {"role_uuid": _("This query parameter is required.")}
            )
        try:
            role = self._proposal_role(proposal, role_uuid)
        except (ValueError, DjangoValidationError):
            raise exceptions.ValidationError({"role_uuid": _("Invalid UUID.")})
        self._check_can_manage_role(proposal, role)
        search = (request.query_params.get("query") or "").strip()
        any_user = role.eligible_from == proposal_enums.ProposalRoleEligibility.ANY_USER
        # Any user of the deployment is eligible: list nobody until asked by
        # enough of a name to look someone up, not to browse the user base.
        if any_user and len(search) < self.CANDIDATES_MIN_QUERY:
            return response.Response([], status=status.HTTP_200_OK)
        users = proposal_roles.eligible_users(role, proposal).exclude(
            id__in=proposal_roles.applicant_team_user_ids(proposal)
        )
        show_detail = self._sees_evaluator_detail(proposal)
        # Overseeing the call shows the email of the call's own people, not
        # of every user of the deployment: for a role anyone may hold, only
        # staff see it.
        show_email = show_detail and (not any_user or request.user.is_staff)
        if search:
            # Only those who see the candidates' email search by part of it;
            # anyone else finds someone by their whole address, so the search
            # tells nothing about addresses it does not show.
            users = users.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | (
                    Q(email__icontains=search)
                    if show_email
                    else Q(email__iexact=search)
                )
                | Q(username__icontains=search)
            )
        limit = self.ANY_USER_CANDIDATES_LIMIT if any_user else self.CANDIDATES_LIMIT
        candidates = list(
            users.order_by("first_name", "last_name", "username").distinct()[:limit]
        )
        candidate_ids = [user.id for user in candidates]
        held = {
            user_id: role_name
            for user_id, role_name in proposal_roles.active_assignments(proposal)
            .filter(user_id__in=candidate_ids)
            .values_list("user_id", "role__name")
        }
        holders_of_role = set(
            proposal_roles.active_assignments(proposal, role)
            .filter(user_id__in=candidate_ids)
            .values_list("user_id", flat=True)
        )
        conflicted = proposal_roles.users_with_recorded_conflicts(proposal)
        with_profiles = set(
            models.ReviewerProfile.objects.filter(
                user_id__in=candidate_ids
            ).values_list("user_id", flat=True)
        )
        data = [
            {
                "uuid": user.uuid,
                "full_name": user.full_name,
                "email": user.email if show_email else None,
                "username": user.username,
                "holds_role": user.id in holders_of_role,
                "holds_other_role": user.id in held and user.id not in holders_of_role,
                "held_role_name": held.get(user.id),
                # Conflicts of interest are the call team's to know, as on
                # the role assignments.
                "has_recorded_conflict": (
                    user.id in conflicted if show_detail else None
                ),
                "has_reviewer_profile": user.id in with_profiles,
            }
            for user in candidates
        ]
        return response.Response(
            serializers.ProposalRoleCandidateSerializer(data, many=True).data,
            status=status.HTTP_200_OK,
        )

    role_assignment_candidates_permissions = [
        proposal_permissions.can_view_proposal_roles
    ]

    @staticmethod
    def _named_step(proposal, step_uuid):
        """The proposal's step instance a step action names, or None."""
        step_uuid = proposal_permissions.parse_step_uuid(step_uuid)
        if step_uuid is None:
            return None
        return (
            proposal.workflow_step_instances.select_related("call_step")
            .filter(uuid=step_uuid)
            .first()
        )

    @extend_schema(
        description=(
            "Complete an active workflow step with an outcome: the one step_uuid "
            "names, as several steps of a stage may be active at once."
        ),
        request=serializers.CompleteWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.CompleteWorkflowStepResponseSerializer
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def complete_workflow_step(self, request, uuid=None):
        proposal = self.get_object()

        if not workflow_service.in_workflow(proposal):
            return response.Response(
                {"detail": "Proposal must be in review state."},
                status=status.HTTP_409_CONFLICT,
            )

        if not proposal.current_step_id:
            return response.Response(
                {"detail": "Proposal has no active workflow step."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The step acted on is the one the request names: several may be
        # active at once. Its call step validates the outcome against the
        # step's outcome set.
        named_step = self._named_step(proposal, request.data.get("step_uuid"))
        if named_step is not None and named_step.call_step.round_scoped:
            raise exceptions.ValidationError(
                {
                    "detail": _(
                        "This step is completed for the whole round: by its "
                        "round reports, or by the call's managers for the round."
                    )
                }
            )
        # A step no longer active is refused below as stale (409); until
        # then the outcome is read against the step the proposal stands in.
        outcome_step = (
            named_step
            if named_step is not None
            and named_step.status == WorkflowStepInstanceStatuses.ACTIVE
            else proposal.current_step
        )
        input_serializer = serializers.CompleteWorkflowStepSerializer(
            data=request.data,
            context={"active_step": outcome_step.call_step},
        )
        input_serializer.is_valid(raise_exception=True)

        outcome = input_serializer.validated_data["outcome"]
        client_step_uuid = input_serializer.validated_data["step_uuid"]
        outcome_reason = input_serializer.validated_data.get("outcome_reason", "")
        # Drop internal_notes for callers who can't read it back. The
        # responsible-role check on this action lets an applicant act on
        # applicant-owned steps (e.g. award_response); without this guard
        # they could silently inject text into a field the call-management
        # team treats as internal.
        internal_notes = (
            input_serializer.validated_data.get("internal_notes", "")
            # A panel member completing their step reads its notes back.
            if named_step is not None
            and proposal_permissions.user_can_view_internal_notes(
                request.user, proposal, named_step, staff=has_staff_access(request)
            )
            else ""
        )

        with transaction.atomic():
            # Lock the proposal row first so complete/reject/advance serialise
            # on the same resource, in a consistent lock order (proposal then
            # step instance) to avoid deadlocks.
            models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            # Lock the step row to serialise concurrent completions: the
            # active step the request names, whichever of its stage it is.
            current_instance = (
                proposal.workflow_step_instances.select_for_update()
                .filter(
                    status=WorkflowStepInstanceStatuses.ACTIVE,
                    uuid=client_step_uuid,
                )
                .first()
            )
            if current_instance is None:
                return response.Response(
                    {"detail": "Workflow step has changed; refresh and retry."},
                    status=status.HTTP_409_CONFLICT,
                )
            if not proposal_permissions.user_may_act_on_locked_step(
                request.user, current_instance
            ):
                raise exceptions.PermissionDenied(
                    "You don't hold the role required to act on the active workflow step."
                )
            # Answering an applicant's step for them is an exception, and the
            # applicant is told so: say why.
            if (
                proposal_roles.answers_for_applicant(request.user, current_instance)
                and not outcome_reason.strip()
            ):
                raise exceptions.ValidationError(
                    {
                        "outcome_reason": _(
                            "Say why you answer this step for the applicant: "
                            "they are told who answered it, and why."
                        )
                    }
                )

            in_run = current_instance.run_id is not None
            try:
                next_instance = workflow_service.complete_step(
                    proposal=proposal,
                    current_instance=current_instance,
                    outcome=outcome,
                    outcome_reason=outcome_reason,
                    completed_by=request.user,
                    internal_notes=internal_notes,
                    budget_override_reason=input_serializer.validated_data[
                        "budget_override_reason"
                    ],
                )
            except ValueError as e:
                # Step gate not satisfied (e.g. too few reviews / score below
                # threshold). Roll back and surface the reason to the caller.
                raise exceptions.ValidationError({"detail": str(e)})
            # Step-activation notifications were intentionally removed; see
            # commit d2d5fb77c "Drop step-activation notifications [WAL-9346]".
            # Re-introducing them requires a debounce/digest policy first.

        if next_instance is None:
            proposal.refresh_from_db()
            if proposal.decision_held:
                return response.Response(
                    serializers.CompleteWorkflowStepResponseSerializer(
                        {"detail": HELD_DECISION_DETAIL}
                    ).data,
                    status=status.HTTP_200_OK,
                )
            running = list(workflow_service.active_instances(proposal))
            if running and workflow_service.in_workflow(proposal):
                # Other steps of its stage are still running.
                names = ", ".join(i.call_step.name or i.step for i in running)
                return response.Response(
                    serializers.CompleteWorkflowStepResponseSerializer(
                        {"detail": f"Step completed. Still running: {names}."}
                    ).data,
                    status=status.HTTP_200_OK,
                )
            if workflow_service.is_awaiting_manual_advance(proposal):
                response_serializer = (
                    serializers.CompleteWorkflowStepResponseSerializer(
                        {"detail": "Step completed. Awaiting manual advance."}
                    )
                )
                return response.Response(
                    response_serializer.data, status=status.HTTP_200_OK
                )
            if in_run:
                # A post-award run ended; the proposal stays accepted and its
                # decision was announced long ago.
                return response.Response(
                    serializers.CompleteWorkflowStepResponseSerializer(
                        {
                            "detail": "Step completed. The run is closed.",
                            "proposal_state": proposal.state,
                        }
                    ).data,
                    status=status.HTTP_200_OK,
                )
            # Terminal step reached: complete_step set the final state — accepted
            # (and provisioned), or rejected / canceled on a negative outcome.
            # Notify with the actual outcome so a rejected/canceled terminal
            # never sends an "accepted" email.
            proposal.refresh_from_db()
            tasks.notify_proposal_decision(
                proposal.uuid, ProposalStates.IN_REVIEW, proposal.state
            )
            response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
                {
                    "detail": _terminal_workflow_detail(proposal.state),
                    "proposal_state": proposal.state,
                }
            )
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )

        response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
            {
                "detail": f"Step completed. Advanced to: {next_instance.call_step.name}",
                "next_step": next_instance.step,
                "next_step_uuid": next_instance.call_step.uuid,
            }
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    complete_workflow_step_serializer_class = serializers.CompleteWorkflowStepSerializer
    complete_workflow_step_permissions = [
        proposal_permissions.can_act_on_active_workflow_step
    ]

    @extend_schema(
        description=(
            "Where the proposal's award stands against the call's budgets for "
            "its round: per budget row, the offered total, what the other "
            "proposals were awarded, what remains, this award and whether it "
            "fits. For the call team; never the applicant team."
        ),
        responses={status.HTTP_200_OK: serializers.ProposalBudgetSerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def budget(self, request, uuid=None):
        proposal = self.get_object()
        return response.Response(
            serializers.ProposalBudgetSerializer(
                {
                    "budget_enforcement": proposal.round.call.budget_enforcement,
                    "entries": budgets.proposal_budget(proposal),
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    budget_permissions = [proposal_permissions.can_view_proposal_budget]

    @extend_schema(
        description=(
            "Reject the proposal at an active workflow step: the one step_uuid "
            "names. Steps running beside it are withdrawn."
        ),
        request=serializers.RejectWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.RejectWorkflowStepResponseSerializer
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def reject_workflow_step(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.RejectWorkflowStepSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        if not workflow_service.in_workflow(proposal):
            return response.Response(
                {"detail": "Proposal must be in review state."},
                status=status.HTTP_409_CONFLICT,
            )

        client_step_uuid = input_serializer.validated_data["step_uuid"]
        reason = input_serializer.validated_data["reason"]
        named_step = self._named_step(proposal, client_step_uuid)
        # See complete_workflow_step for the symmetric write-side gate: only
        # call-management-team users may persist internal_notes; applicants
        # acting on their own step would otherwise leak private text.
        internal_notes = (
            input_serializer.validated_data.get("internal_notes", "")
            # A panel member completing their step reads its notes back.
            if named_step is not None
            and proposal_permissions.user_can_view_internal_notes(
                request.user, proposal, named_step, staff=has_staff_access(request)
            )
            else ""
        )

        with transaction.atomic():
            # Lock the proposal row first (consistent order with
            # complete/advance) to serialise concurrent workflow mutations.
            models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            current_instance = (
                proposal.workflow_step_instances.select_for_update()
                .filter(
                    status=WorkflowStepInstanceStatuses.ACTIVE,
                    uuid=client_step_uuid,
                )
                .first()
            )
            if current_instance is None:
                return response.Response(
                    {"detail": "Workflow step has changed; refresh and retry."},
                    status=status.HTTP_409_CONFLICT,
                )
            if not proposal_permissions.user_may_act_on_locked_step(
                request.user, current_instance, rejecting=True
            ):
                raise exceptions.PermissionDenied(
                    "You don't hold the role required to reject at this workflow step."
                )
            if workflow_service.open_change_request(current_instance) is not None:
                return response.Response(
                    {
                        "detail": (
                            "Changes have been requested of the applicant at "
                            "this step: cancel the request before rejecting."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            in_run = current_instance.run_id is not None
            try:
                workflow_service.reject_at_step(
                    proposal=proposal,
                    current_instance=current_instance,
                    reason=reason,
                    completed_by=request.user,
                    internal_notes=internal_notes,
                )
            except ValueError as e:
                raise exceptions.ValidationError({"detail": str(e)})

        proposal.refresh_from_db()
        if in_run:
            return response.Response(
                serializers.RejectWorkflowStepResponseSerializer(
                    {"detail": "The run is closed.", "proposal_state": proposal.state}
                ).data,
                status=status.HTTP_200_OK,
            )
        if proposal.decision_held:
            response_serializer = serializers.RejectWorkflowStepResponseSerializer(
                {"detail": HELD_DECISION_DETAIL, "proposal_state": proposal.state}
            )
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )
        response_serializer = serializers.RejectWorkflowStepResponseSerializer(
            {"detail": "Proposal rejected.", "proposal_state": ProposalStates.REJECTED}
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    reject_workflow_step_serializer_class = serializers.RejectWorkflowStepSerializer
    reject_workflow_step_permissions = [
        proposal_permissions.can_reject_at_active_workflow_step
    ]

    @extend_schema(
        description=(
            "Manually advance a workflow that is awaiting call-manager confirmation."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.CompleteWorkflowStepResponseSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def advance_workflow_step(self, request, uuid=None):
        proposal = self.get_object()

        if not workflow_service.in_workflow(proposal):
            return response.Response(
                {"detail": "Proposal must be in review state."},
                status=status.HTTP_409_CONFLICT,
            )

        from_step = proposal.workflow_step
        actor = request.user.full_name or request.user.username

        with transaction.atomic():
            # Lock the proposal row to serialise concurrent advances.
            locked = models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            # Re-check state under the lock: a concurrent transition could
            # have moved the proposal out of IN_REVIEW between the pre-lock
            # read above and the lock acquisition here.
            if not workflow_service.in_workflow(locked):
                return response.Response(
                    {"detail": "Proposal must be in review state."},
                    status=status.HTTP_409_CONFLICT,
                )
            # And who the user is to it: they may have joined its team.
            if not (
                request.user.is_staff
                or proposal_permissions.manages_proposal_workflow(request.user, locked)
            ):
                raise exceptions.PermissionDenied(
                    "Only the call manager can advance this workflow step."
                )
            in_run = runs.open_run_of(locked) is not None
            try:
                next_instance = workflow_service.advance_step(
                    proposal=locked, acting_user=request.user
                )
            except ValueError as e:
                return response.Response(
                    {"detail": str(e)}, status=status.HTTP_409_CONFLICT
                )

            target = (
                "workflow completion" if next_instance is None else next_instance.step
            )
            # Pass user-controlled strings as named placeholders in
            # event_context rather than f-string interpolation. The emit
            # helper calls .format(**context) on the template, so an f-string
            # that already contains a name like "{user_token_lifetime}" would
            # otherwise resolve against the event context at emit time.
            event_logger.emit(
                "Proposal {proposal_name} workflow manually advanced "
                "from {from_step} to {target} by {actor}.",
                event_type=EventType.PROPOSAL_WORKFLOW_ADVANCED,
                event_context={
                    "proposal": locked,
                    "from_step": from_step,
                    "target": target,
                    "actor": actor,
                },
                scopes=[locked],
            )

        if next_instance is None and in_run:
            proposal.refresh_from_db()
            return response.Response(
                serializers.CompleteWorkflowStepResponseSerializer(
                    {
                        "detail": "Advanced. The run is closed.",
                        "proposal_state": proposal.state,
                    }
                ).data,
                status=status.HTTP_200_OK,
            )
        if next_instance is None:
            proposal.refresh_from_db()
            if proposal.decision_held:
                # Advanced onto an automatic allocation decision that the
                # round holds until it publishes.
                return response.Response(
                    serializers.CompleteWorkflowStepResponseSerializer(
                        {"detail": HELD_DECISION_DETAIL}
                    ).data,
                    status=status.HTTP_200_OK,
                )
            # Manual advance reached the end of the workflow, directly or
            # through an automatic allocation decision: accepted and
            # provisioned inside advance_step. Fire the shared decision
            # notifications.
            tasks.notify_proposal_decision(
                proposal.uuid, ProposalStates.IN_REVIEW, proposal.state
            )
            response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
                {
                    "detail": _terminal_workflow_detail(proposal.state),
                    "proposal_state": proposal.state,
                }
            )
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )

        response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
            {
                "detail": f"Advanced to: {next_instance.call_step.name}",
                "next_step": next_instance.step,
                "next_step_uuid": next_instance.call_step.uuid,
            }
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    advance_workflow_step_permissions = [proposal_permissions.can_advance_workflow_step]

    @extend_schema(
        description=(
            "Reopen an allocation decision held for the round's publication: "
            "the decision step becomes active again with its outcome cleared, "
            "and the proposal counts as undecided until it is decided anew. "
            "Only before the round's results are published. Nothing is sent "
            "to the applicant. The reopen is logged on the proposal's event "
            "feed, which the applicant team reads, so the event carries "
            "neither the reason nor the outcome taken back."
        ),
        request=serializers.ReopenDecisionSerializer,
        responses={
            status.HTTP_200_OK: serializers.ProposalWorkflowStepInstanceSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def reopen_decision(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.ReopenDecisionSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        try:
            instance = workflow_service.reopen_held_decision(
                proposal,
                request.user,
                reason=input_serializer.validated_data["reason"],
                deadline=input_serializer.validated_data["deadline"],
            )
        except workflow_service.DecisionNotReopenable as e:
            return response.Response(
                {"detail": str(e)}, status=status.HTTP_409_CONFLICT
            )

        # Only the call's managers get here, so the step is shown whole.
        output = serializers.ProposalWorkflowStepInstanceSerializer(
            instance,
            context={
                "can_view_internal_notes": True,
                "can_view_step_actors": True,
                "can_view_review_content": True,
            },
        )
        return response.Response(output.data, status=status.HTTP_200_OK)

    reopen_decision_serializer_class = serializers.ReopenDecisionSerializer
    reopen_decision_permissions = [proposal_permissions.can_reopen_held_decision]

    # Requests for changes

    def _change_request_context(self, proposal):
        """Who asked for changes is not told to the applicant team: the
        step's actor may be one of its evaluators."""
        on_team = proposal_roles.is_on_applicant_team(self.request.user, proposal)
        return {
            "conceals_requester": lambda change_request: (
                on_team and not self.request.user.is_staff
            )
        }

    @extend_schema(
        description=(
            "Send a proposal in review back to its applicant to change the "
            "parts named, at a step that allows it. The proposal stays in "
            "review on the step; the parts become editable for the applicant "
            "until the changes are submitted, and the step does not lapse "
            "meanwhile. For whoever acts on the step -- the call's managers "
            "and organizers, its completer -- and staff; never the applicant "
            "team. Changes may be requested again after each submission."
        ),
        request=serializers.RequestChangesSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalChangeRequestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def request_changes(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.RequestChangesSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        with transaction.atomic():
            # Lock order as for the step actions: proposal, then step.
            proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            instance = (
                proposal.workflow_step_instances.select_for_update(of=("self",))
                .select_related("call_step")
                .filter(uuid=data["step_uuid"])
                .first()
            )
            if instance is None:
                raise exceptions.ValidationError(
                    {"step_uuid": _("The proposal has no such workflow step.")}
                )
            if not proposal_permissions.user_can_request_changes(
                request.user, proposal, instance.call_step
            ):
                raise exceptions.PermissionDenied(
                    "You don't act on this workflow step."
                )
            try:
                change_request = workflow_service.request_changes(
                    proposal,
                    instance,
                    request.user,
                    message=data["message"],
                    parts=data["parts"],
                    due_at=data["due_at"],
                )
            except workflow_service.ChangeRequestError as e:
                raise exceptions.ValidationError({"detail": str(e)})
        return response.Response(
            serializers.ProposalChangeRequestSerializer(change_request).data,
            status=status.HTTP_200_OK,
        )

    request_changes_permissions = []
    request_changes_serializer_class = serializers.RequestChangesSerializer

    @extend_schema(
        description=(
            "Submit the changes requested of the applicant, every open request "
            "at once: several steps of a stage may each have asked. The parts "
            "unlocked are locked again, and each asking step goes back to its "
            "actor with its full duration. The proposal keeps its submission "
            "time. Returns the requests closed. For those who may submit the "
            "proposal."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.ProposalChangeRequestSerializer(many=True)
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def submit_changes(self, request, uuid=None):
        proposal = self.get_object()
        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            pending = list(change_requests.open_applicant_requests(proposal))
            if not pending:
                return response.Response(
                    {"detail": _("No changes have been requested of this proposal.")},
                    status=status.HTTP_409_CONFLICT,
                )
            # What a submission must carry, but the round's cut-off: the
            # proposal was submitted in time. Changes to a post-award run leave the accepted
            # proposal as it was.
            if pending[0].step_instance.run_id is None:
                validate_submitted_changes(
                    proposal,
                    {
                        part
                        for request in pending
                        for part in request.unlocked_parts or []
                    },
                )
            try:
                closed = workflow_service.submit_changes(proposal, request.user)
            except workflow_service.ChangeRequestError as e:
                raise exceptions.ValidationError({"detail": str(e)})
        return response.Response(
            serializers.ProposalChangeRequestSerializer(
                closed, many=True, context=self._change_request_context(proposal)
            ).data,
            status=status.HTTP_200_OK,
        )

    submit_changes_permissions = [proposal_permissions.can_manage_proposal]

    @extend_schema(
        description=(
            "Call off an open request for changes to the applicant: the step is "
            "its actor's again, with its full duration. For whoever made the "
            "request, the call's managers and organizers, and staff."
        ),
        request=serializers.CancelChangeRequestSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalChangeRequestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def cancel_change_request(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.CancelChangeRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        user = request.user
        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            change_request = (
                proposal.change_requests.select_related("step_instance__call_step")
                .filter(uuid=input_serializer.validated_data["change_request_uuid"])
                .first()
            )
            if change_request is None or not (
                proposal_permissions.user_reads_change_request(user, change_request)
            ):
                raise exceptions.NotFound()
            made_it = (
                change_request.requested_by_id == user.id
                and not proposal_roles.is_on_applicant_team(user, proposal)
            )
            if not (
                made_it
                or proposal_permissions.user_oversees_contributions(user, proposal)
            ):
                raise exceptions.PermissionDenied(
                    "Only who asked for the changes, or the call's managers, "
                    "call them off."
                )
            try:
                change_request = workflow_service.cancel_change_request(
                    proposal, change_request, user
                )
            except workflow_service.ChangeRequestError as e:
                raise exceptions.ValidationError({"detail": str(e)})
        return response.Response(
            serializers.ProposalChangeRequestSerializer(change_request).data,
            status=status.HTTP_200_OK,
        )

    cancel_change_request_permissions = []
    cancel_change_request_serializer_class = serializers.CancelChangeRequestSerializer

    @extend_schema(
        description=(
            "The requests for changes made on the proposal, open and closed, "
            "oldest first: the rounds of changes asked of its applicant, and "
            "the contributions reopened. Each as far as the caller reads it: "
            "the applicant team the requests made to it, the step's actors and "
            "the call's managers those, and a reopened contribution its "
            "contributors and the call's managers."
        ),
        responses={
            status.HTTP_200_OK: serializers.ProposalChangeRequestSerializer(many=True)
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def change_requests(self, request, uuid=None):
        proposal = self.get_object()
        access = contributions.Access(request.user)
        readable = [
            change_request
            for change_request in proposal.change_requests.select_related(
                "step_instance__call_step",
                "requested_by",
                "contribution__requested_offering",
                "contribution__proposal_step__call_step",
            )
            if proposal_permissions.user_reads_change_request(
                request.user, change_request, access
            )
        ]
        serializer = serializers.ProposalChangeRequestSerializer(
            readable, many=True, context=self._change_request_context(proposal)
        )
        return response.Response(serializer.data)

    change_requests_permissions = []

    @extend_schema(
        description=(
            "The parts of the proposal a request for changes may unlock: the "
            "Project details fields the call collects, and its documents, "
            "requested resources, team and compliance answers. At a step of "
            "an extension request or final report (step_uuid), the run's "
            "parts instead: its applicant step's answers, and what an "
            "extension asks for."
        ),
        parameters=[
            OpenApiParameter(
                name="step_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description="The step instance the changes would be asked at.",
                extensions={
                    "x-waldur-operation-id": ("proposal_proposals_workflow_states_list")
                },
            )
        ],
        responses={status.HTTP_200_OK: serializers.UnlockablePartsSerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def unlockable_parts(self, request, uuid=None):
        proposal = self.get_object()
        instance = self._named_step(proposal, request.query_params.get("step_uuid"))
        return response.Response(
            {"parts": change_requests.unlockable_parts(proposal, instance)},
            status=status.HTTP_200_OK,
        )

    unlockable_parts_permissions = []

    @extend_schema(
        description=(
            "Reopen a submitted contribution -- one offering's technical "
            "assessment, say -- for its contributor to revise and submit "
            "again. While its step is active, or completed with the proposal "
            "waiting to be advanced by hand, in which case the step becomes "
            "active again. Its answers are kept; its outcome counts again once "
            "resubmitted. Not for reviews. For the call's managers and "
            "organizers, and staff. The applicant is not told."
        ),
        request=serializers.ReopenContributionSerializer,
        responses={status.HTTP_200_OK: serializers.ProposalChangeRequestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def reopen_contribution(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.ReopenContributionSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        if not proposal_permissions.user_oversees_contributions(request.user, proposal):
            raise exceptions.PermissionDenied(
                "Only the call's managers reopen a contribution."
            )
        with transaction.atomic():
            # Lock order as for the step actions: proposal, step, contribution.
            proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            instance_id = (
                models.StepContribution.objects.filter(
                    proposal_step__proposal=proposal, uuid=data["contribution_uuid"]
                )
                .values_list("proposal_step_id", flat=True)
                .first()
            )
            if instance_id is None:
                raise exceptions.NotFound()
            instance = (
                models.ProposalWorkflowStepInstance.objects.select_for_update(
                    of=("self",)
                )
                .select_related("call_step")
                .get(pk=instance_id)
            )
            contribution = models.StepContribution.objects.select_for_update().get(
                proposal_step=instance, uuid=data["contribution_uuid"]
            )
            try:
                change_request = workflow_service.reopen_contribution(
                    proposal, instance, contribution, request.user, data["message"]
                )
            except workflow_service.ChangeRequestError as e:
                raise exceptions.ValidationError({"detail": str(e)})
        return response.Response(
            serializers.ProposalChangeRequestSerializer(change_request).data,
            status=status.HTTP_200_OK,
        )

    reopen_contribution_permissions = []
    reopen_contribution_serializer_class = serializers.ReopenContributionSerializer

    @extend_schema(
        request=serializers.ProposalDetachDocumentsSerializer,
        responses=None,
        description="Detach documents from proposal.",
    )
    @decorators.action(detail=True, methods=["post"])
    def detach_documents(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        # Handle both JSON (list) and form data (QueryDict with getlist)
        if hasattr(request.data, "getlist"):
            documents = request.data.getlist("documents", [])
        else:
            documents = request.data.get("documents", [])
        for doc_uuid in documents:
            try:
                doc = models.ProposalDocumentation.objects.get(
                    proposal=proposal,
                    uuid=doc_uuid,
                )
                file_name = _document_name(doc)
                doc.delete()
                event_logger.emit(
                    "{actor_name} removed {file_name} from proposal {proposal_name}.",
                    event_type=EventType.PROPOSAL_DOCUMENT_REMOVED,
                    event_context={
                        "proposal": proposal,
                        "actor_name": request.user.full_name or request.user.username,
                        "file_name": file_name,
                    },
                    scopes=[proposal],
                )
                logger.info(f"Attachment for {proposal.name} has been removed.")
            except models.ProposalDocumentation.DoesNotExist:
                pass  # Skip non-existent documents

        return response.Response(
            "Documents removed successfully",
            status=status.HTTP_200_OK,
        )

    detach_documents_serializer_class = serializers.ProposalDetachDocumentsSerializer
    detach_documents_permissions = [
        proposal_permissions.can_update_proposal_attachments
    ]

    # NOTE: the legacy one-click `approve`/`reject` actions were removed — every
    # proposal is now driven through the workflow engine (complete/advance/reject
    # workflow-step actions), which is the single provisioning path. See the
    # data migration that backfilled any pre-engine `submitted`/`in_review`
    # proposals with workflow instances.

    # Checklist Integration Endpoints
    # Checklist methods are now provided by ChecklistViewSetMixin
    # - checklist: Get checklist with questions and existing answers
    # - submit_answers: Submit checklist answers (overridden below)
    # - completion_status: Get completion status

    @extend_schema(
        description="Submit checklist answers.",
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={
            200: serializers.ProposalChecklistAnswerSubmitResponseSerializer,
            400: {"description": "Validation error or no checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def submit_answers(self, request, uuid=None):
        """Submit checklist answers with proposal-specific response that includes review status."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate input data
        submit_serializer = checklist_serializers.AnswerSubmitSerializer(
            data=request.data,
            many=True,
            context={"completion": completion, "request": request},
        )
        submit_serializer.is_valid(raise_exception=True)

        # Process each answer
        for answer_data in submit_serializer.validated_data:
            question = answer_data["question"]
            answer_value = answer_data["answer_data"]

            if answer_value is None:
                # Remove answer (hard delete)
                checklist_models.Answer.objects.filter(
                    completion=completion,
                    question=question,
                    user=request.user,
                ).delete()
            else:
                # Create or update answer using direct foreign key
                checklist_models.Answer.objects.update_or_create(
                    completion=completion,
                    question=question,
                    user=request.user,
                    defaults={"answer_data": answer_value},
                )

        # Update completion status to reflect any changes from additions/removals
        completion.update_completion_status()
        self._note_change_for_applicant(request, obj, "Compliance answers")

        # Return updated completion status
        completion.refresh_from_db()

        # Create response data with proposal-specific serializer that includes review status
        response_data = {
            "detail": "Answers submitted successfully",
            "completion": completion,
        }

        response_serializer = (
            serializers.ProposalChecklistAnswerSubmitResponseSerializer(
                response_data, context={"request": request}
            )
        )

        return response.Response(response_serializer.data)

    # -- Per-step checklists (WAL-9484) -----------------------------------
    # The actions above are hardwired to the single call-level compliance
    # checklist. These step-parameterized variants address the checklist
    # attached to an individual workflow step (CallWorkflowStep.checklist),
    # reusing the same request/response serializers so the frontend renders
    # them with the same components.

    def _resolve_step_checklist(self, request, obj):
        """Return (call_step, error_response). error_response is None on success."""
        step_uuid, step_key = proposal_permissions.get_step_query_params(request)
        if not step_uuid and not step_key:
            return None, response.Response(
                {"detail": "The 'step' or 'step_uuid' query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        call_step = proposal_permissions.resolve_call_step(obj, step_uuid, step_key)
        if call_step is None or not call_step.checklist_id:
            return None, response.Response(
                {"detail": "No checklist is configured for this workflow step."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return call_step, None

    def _resolve_step_contribution(self, request, obj, call_step, create=True):
        """Return (contribution, error_response) for a step's form.

        The contribution named by ``?contribution_uuid=``, else the caller's
        own, made on demand where the step makes slots that way and
        ``create`` is set. Without ``create`` a caller with no contribution
        gets (None, None).
        """
        instance = proposal_permissions.step_instance_for(obj, call_step)
        if instance is None:
            return None, response.Response(
                {"detail": "The proposal has not reached this workflow step."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            contribution = contributions.resolve_contribution(
                request.user,
                instance,
                contribution_uuid=proposal_permissions.get_contribution_query_param(
                    request
                ),
                create=create,
            )
        except contributions.ContributionError as e:
            return None, response.Response(
                {"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )
        if contribution is None and create:
            return None, response.Response(
                {
                    "detail": (
                        "You have no contribution to this workflow step; name "
                        "one with contribution_uuid."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return contribution, None

    @extend_schema(
        description="Get a workflow step's checklist with questions and answers.",
        parameters=[
            OpenApiParameter(
                name="step",
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Key of the call step (e.g. technical_assessment). Either "
                    "this or step_uuid is required."
                ),
            ),
            OpenApiParameter(
                name="step_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description="UUID of the call step.",
                extensions={
                    "x-waldur-operation-id": (
                        "proposal_protected_calls_workflow_steps_list"
                    )
                },
            ),
            OpenApiParameter(
                name="contribution_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description=(
                    "UUID of the contribution whose answers are meant. The "
                    "caller's own contribution to the step when left out."
                ),
                extensions={
                    "x-waldur-operation-id": "proposal_proposals_contributions_list"
                },
            ),
            OpenApiParameter(
                name="include_all",
                location=OpenApiParameter.QUERY,
                required=False,
                description="Return all questions ignoring dynamic visibility.",
            ),
        ],
        responses={
            200: checklist_serializers.ChecklistResponseSerializer,
            400: {"description": "No step/checklist"},
        },
    )
    @decorators.action(detail=True, methods=["get"], url_path="step-checklist")
    def step_checklist(self, request, uuid=None):
        obj = self.get_object()
        call_step, error = self._resolve_step_checklist(request, obj)
        if error is not None:
            return error
        checklist = call_step.checklist
        # Reading makes nothing: a caller without a contribution of their own
        # gets the blank form, and their slot is made when they first answer.
        contribution, error = self._resolve_step_contribution(
            request, obj, call_step, create=False
        )
        if error is not None:
            return error
        access = contributions.access_for(request)
        if contribution is not None and not access.can_read(contribution):
            raise exceptions.PermissionDenied(
                "You do not have permission to view this contribution."
            )
        completion = (
            contribution.get_checklist_completion(checklist)
            if contribution is not None
            else None
        )
        # Each contribution has a completion of its own, so the answers shown
        # are the contribution's, whoever of its team gave them.
        answers = completion.get_latest_answers() if completion is not None else {}
        form_completion = completion or checklist_models.ChecklistCompletion(
            checklist=checklist
        )
        query_params = getattr(request, "query_params", request.GET)
        include_all = query_params.get("include_all", "false").lower() == "true"
        if include_all:
            questions = checklist.get_questions()
        else:
            questions = checklist.get_visible_questions(
                form_completion, answers=answers
            )
        context = {"request": request, "completion": form_completion}
        if completion is not None:
            data = checklist_serializers.ChecklistResponseSerializer(
                {
                    "checklist": checklist,
                    "completion": completion,
                    "questions": questions,
                },
                context=context,
            ).data
        else:
            data = self._blank_step_checklist(checklist, questions, context)
        if contribution is not None and access.conceals_contributor(contribution):
            for question in data["questions"]:
                answer = question.get("existing_answer")
                if answer:
                    answer.pop("user", None)
                    answer.pop("user_name", None)
        return response.Response(data)

    @staticmethod
    def _blank_step_checklist(checklist, questions, context):
        """A step's form with no answers, for a caller yet to answer it."""
        required = [question for question in questions if question.required]
        return {
            "checklist": {
                "uuid": str(checklist.uuid),
                "name": checklist.name,
                "description": checklist.description,
                "checklist_type": checklist.checklist_type,
            },
            "completion": {
                "uuid": None,
                "is_completed": not required,
                "completion_percentage": 0 if questions else 100,
                "unanswered_required_questions": [
                    {
                        "uuid": str(question.uuid),
                        "description": question.description,
                        "question_type": question.question_type,
                    }
                    for question in required
                ],
                "checklist_name": checklist.name,
                "checklist_description": checklist.description,
                "created": None,
                "modified": None,
            },
            "questions": checklist_serializers.QuestionWithAnswerSerializer(
                questions, many=True, context=context
            ).data,
        }

    step_checklist_permissions = [proposal_permissions.can_read_step_checklist]

    @extend_schema(
        description="Submit answers to a workflow step's checklist.",
        parameters=[
            OpenApiParameter(
                name="step",
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Key of the call step (e.g. technical_assessment). Either "
                    "this or step_uuid is required."
                ),
            ),
            OpenApiParameter(
                name="step_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description="UUID of the call step.",
                extensions={
                    "x-waldur-operation-id": (
                        "proposal_protected_calls_workflow_steps_list"
                    )
                },
            ),
            OpenApiParameter(
                name="contribution_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description=(
                    "UUID of the contribution whose answers are meant. The "
                    "caller's own contribution to the step when left out."
                ),
                extensions={
                    "x-waldur-operation-id": "proposal_proposals_contributions_list"
                },
            ),
        ],
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={
            200: serializers.ProposalChecklistAnswerSubmitResponseSerializer,
            400: {"description": "Validation error or no checklist configured"},
        },
    )
    @decorators.action(
        detail=True, methods=["post"], url_path="submit-step-checklist-answers"
    )
    def submit_step_checklist_answers(self, request, uuid=None):
        obj = self.get_object()
        call_step, error = self._resolve_step_checklist(request, obj)
        if error is not None:
            return error
        contribution, error = self._resolve_step_contribution(request, obj, call_step)
        if error is not None:
            return error
        with transaction.atomic():
            # Lock order as for the step actions: proposal, step,
            # contribution. Whether the caller may still answer is decided
            # under the locks, so a submission, or the step's completion,
            # meanwhile is waited for and then refuses the answers.
            models.Proposal.objects.select_for_update(of=("self",)).get(pk=obj.pk)
            instance = (
                models.ProposalWorkflowStepInstance.objects.select_for_update(
                    of=("self",)
                )
                .select_related("call_step")
                .get(pk=contribution.proposal_step_id)
            )
            contribution = models.StepContribution.objects.select_for_update().get(
                pk=contribution.pk
            )
            contribution.proposal_step = instance
            if not contributions.user_can_fill(request.user, contribution):
                raise exceptions.PermissionDenied(
                    "You cannot answer this contribution, or it is no longer open."
                )
            completion = contribution.ensure_checklist_completion(call_step.checklist)

            submit_serializer = checklist_serializers.AnswerSubmitSerializer(
                data=request.data,
                many=True,
                context={"completion": completion, "request": request},
            )
            submit_serializer.is_valid(raise_exception=True)

            for answer_data in submit_serializer.validated_data:
                question = answer_data["question"]
                answer_value = answer_data["answer_data"]
                if answer_value is None:
                    checklist_models.Answer.objects.filter(
                        completion=completion, question=question, user=request.user
                    ).delete()
                else:
                    checklist_models.Answer.objects.update_or_create(
                        completion=completion,
                        question=question,
                        user=request.user,
                        defaults={"answer_data": answer_value},
                    )

            completion.update_completion_status()
            # Answers to the applicant's own step, given for them.
            if instance.call_step.kind == StepKinds.APPLICANT_ACTION:
                self._note_change_for_applicant(
                    request, obj, f"Answers to step {instance.call_step.name}"
                )
        completion.refresh_from_db()

        response_serializer = (
            serializers.ProposalChecklistAnswerSubmitResponseSerializer(
                {"detail": "Answers submitted successfully", "completion": completion},
                context={"request": request},
            )
        )
        return response.Response(response_serializer.data)

    submit_step_checklist_answers_permissions = [
        proposal_permissions.can_submit_step_checklist_answers
    ]

    @extend_schema(
        description=(
            "List the contributions to a workflow step with their checklist "
            "answers, for the threaded view of the step: one entry per "
            "contribution (per person or per requested offering) the caller "
            "may read."
        ),
        parameters=[
            OpenApiParameter(
                name="step",
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Key of the call step (e.g. technical_assessment). Either "
                    "this or step_uuid is required."
                ),
            ),
            OpenApiParameter(
                name="step_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description="UUID of the call step.",
                extensions={
                    "x-waldur-operation-id": (
                        "proposal_protected_calls_workflow_steps_list"
                    )
                },
            ),
        ],
        responses={
            200: serializers.StepContributionWithAnswersSerializer(many=True),
            400: {"description": "No step/checklist"},
        },
    )
    @decorators.action(
        detail=True, methods=["get"], url_path="step-checklist-responses"
    )
    def step_checklist_responses(self, request, uuid=None):
        obj = self.get_object()
        call_step, error = self._resolve_step_checklist(request, obj)
        if error is not None:
            return error
        instance = proposal_permissions.step_instance_for(obj, call_step)
        if instance is None:
            return response.Response([])
        readable = contributions.readable_contributions(
            request.user, obj, instance, access=contributions.access_for(request)
        )
        serializer = serializers.StepContributionWithAnswersSerializer(
            readable, many=True, context={"request": request}
        )
        return response.Response(serializer.data)

    step_checklist_responses_permissions = [
        proposal_permissions.can_view_step_checklist_responses
    ]

    def _locked_contribution_step(self, proposal, step_uuid, contribution_uuid=None):
        """The proposal's step instance a contribution action is about, locked.

        Named by its call step's uuid, else the step of the named
        contribution, else the proposal's current step -- unless several steps
        of a stage are active, when the request must name one.
        """
        instances = proposal.workflow_step_instances.select_for_update().select_related(
            "call_step"
        )
        if step_uuid:
            return instances.filter(call_step__uuid=step_uuid).first()
        if contribution_uuid:
            return instances.filter(contributions__uuid=contribution_uuid).first()
        if (
            proposal.workflow_step_instances.filter(
                status=WorkflowStepInstanceStatuses.ACTIVE
            ).count()
            > 1
        ):
            raise exceptions.ValidationError(
                {
                    "step_uuid": (
                        "Several workflow steps are active; name the one this is about."
                    )
                }
            )
        if proposal.current_step_id:
            return instances.filter(pk=proposal.current_step_id).first()
        return None

    @extend_schema(
        description=(
            "List the contributions to the proposal's workflow steps the caller "
            "may read: one per person or per requested offering, with status, "
            "outcome and conditions."
        ),
        parameters=[
            OpenApiParameter(
                name="step_uuid",
                location=OpenApiParameter.QUERY,
                required=False,
                type=OpenApiTypes.UUID,
                description="UUID of the call step; every step when left out.",
                extensions={
                    "x-waldur-operation-id": (
                        "proposal_protected_calls_workflow_steps_list"
                    )
                },
            ),
        ],
        responses={200: serializers.StepContributionSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"])
    def contributions(self, request, uuid=None):
        proposal = self.get_object()
        step_uuid, _ = proposal_permissions.get_step_query_params(request)
        instance = None
        if step_uuid:
            try:
                instance = proposal.workflow_step_instances.filter(
                    call_step__uuid=step_uuid
                ).first()
            except (ValueError, DjangoValidationError):
                instance = None
            if instance is None:
                return response.Response([])
        # The person a slot is handed to sees it before accepting it -- its
        # step, its holder and when it is due -- to accept or decline it.
        readable = contributions.readable_contributions(
            request.user,
            proposal,
            instance,
            access=contributions.access_for(request),
            with_handed_over=True,
        )
        serializer = serializers.StepContributionSerializer(
            readable, many=True, context={"request": request}
        )
        return response.Response(serializer.data)

    contributions_permissions = []

    @extend_schema(
        description=(
            "Submit a contribution to a workflow step with its outcome. On a "
            "step whose outcome its contributions decide, the last one "
            "completes the step."
        ),
        request=serializers.SubmitStepContributionSerializer,
        responses={200: serializers.SubmitStepContributionResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def submit_contribution(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.SubmitStepContributionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        with transaction.atomic():
            # Lock order as for the step actions: proposal, step, contribution.
            proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal.pk
            )
            instance = self._locked_contribution_step(
                proposal, data.get("step_uuid"), data.get("contribution_uuid")
            )
            if instance is None:
                raise exceptions.ValidationError(
                    {"detail": "The proposal has no such workflow step."}
                )
            if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
                raise exceptions.ValidationError(
                    {
                        "detail": (
                            f"The step '{instance.call_step.name}' is "
                            f"{instance.status}; its contributions can no "
                            "longer be submitted."
                        )
                    }
                )
            try:
                contribution = contributions.resolve_contribution(
                    request.user,
                    instance,
                    contribution_uuid=data.get("contribution_uuid"),
                )
            except contributions.ContributionError as e:
                raise exceptions.ValidationError({"detail": str(e)})
            if contribution is None:
                raise exceptions.ValidationError(
                    {"detail": "You have no contribution to this workflow step."}
                )
            contribution = models.StepContribution.objects.select_for_update().get(
                pk=contribution.pk
            )
            if not contributions.user_can_fill(request.user, contribution):
                raise exceptions.PermissionDenied(
                    "You cannot submit this contribution, or it is no longer open."
                )
            try:
                contributions.submit(
                    contribution,
                    request.user,
                    outcome=data["outcome"],
                    conditions=data["conditions"],
                    comment=data["comment"],
                )
            except contributions.ContributionError as e:
                raise exceptions.ValidationError({"detail": str(e)})
            activity.log_contribution_submitted(contribution)
            completed, next_instance = workflow_service.complete_from_contributions(
                proposal, instance, request.user
            )

        self._notify_contribution_decision(proposal, completed, next_instance)
        contribution.refresh_from_db()
        response_serializer = serializers.SubmitStepContributionResponseSerializer(
            {
                "detail": (
                    "Contribution submitted. The step is completed."
                    if completed
                    else "Contribution submitted."
                ),
                "contribution": contribution,
                "step_completed": completed,
                "proposal_state": proposal.state,
            },
            context={"request": request},
        )
        return response.Response(response_serializer.data)

    @staticmethod
    def _notify_contribution_decision(proposal, completed, next_instance):
        """Tell the applicant when a contribution completed the workflow."""
        tasks.notify_contribution_decision(proposal, completed, next_instance)

    submit_contribution_permissions = []
    submit_contribution_serializer_class = serializers.SubmitStepContributionSerializer

    @extend_schema(
        description=(
            "Decline a contribution of one's own, or, as a call manager, "
            "withdraw an open contribution from the step's work."
        ),
        request=serializers.DeclineStepContributionSerializer,
        responses={200: serializers.StepContributionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def decline_contribution(self, request, uuid=None):
        proposal = self._proposal_for_delegate(request)
        input_serializer = serializers.DeclineStepContributionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        with transaction.atomic():
            locked_proposal = models.Proposal.objects.select_for_update(
                of=("self",)
            ).get(pk=proposal.pk)
            contribution = (
                models.StepContribution.objects.select_for_update()
                .filter(
                    proposal_step__proposal=proposal, uuid=data["contribution_uuid"]
                )
                .first()
            )
            access = contributions.Access(request.user)
            if contribution is None or not (
                access.can_read(contribution) or access.is_delegate(contribution)
            ):
                raise exceptions.NotFound()
            handed_back_by = (
                request.user if contribution.delegate_id == request.user.pk else None
            )
            delegated_by = contribution.delegated_by
            try:
                contributions.decline(contribution, request.user, data["comment"])
            except contributions.ContributionError as e:
                raise exceptions.ValidationError({"detail": str(e)})
            if handed_back_by is not None and delegated_by is not None:
                transaction.on_commit(
                    lambda: tasks.notify_contribution_delegated.delay(
                        contribution.uuid.hex,
                        tasks.DelegationEvents.DECLINED,
                        delegated_by.uuid.hex,
                        handed_back_by.uuid.hex,
                    )
                )
            # Taking out the last open contribution may be what completes it.
            instance = (
                models.ProposalWorkflowStepInstance.objects.select_for_update()
                .select_related("call_step")
                .get(pk=contribution.proposal_step_id)
            )
            completed, next_instance = workflow_service.complete_from_contributions(
                locked_proposal, instance, request.user
            )
        self._notify_contribution_decision(proposal, completed, next_instance)
        serializer = serializers.StepContributionSerializer(
            contribution, context={"request": request}
        )
        return response.Response(serializer.data)

    decline_contribution_permissions = []
    decline_contribution_serializer_class = (
        serializers.DeclineStepContributionSerializer
    )

    def _proposal_for_delegate(self, request):
        """The proposal, as ``get_object`` finds it -- or, for the person a
        per-offering slot of it is handed to and who has yet to accept it,
        which opens the proposal to them, the proposal that slot is on: they
        answer it from their task list. Anyone else gets the usual 404."""
        try:
            return self.get_object()
        except Http404:
            contribution_uuid = request.data.get("contribution_uuid")
            try:
                contribution = (
                    models.StepContribution.objects.filter(
                        proposal_step__proposal__uuid=self.kwargs.get("uuid"),
                        uuid=contribution_uuid,
                        delegate=request.user,
                    )
                    .select_related(
                        "proposal_step__call_step",
                        "proposal_step__proposal__round__call",
                        "requested_offering",
                        "delegate",
                    )
                    .first()
                )
            except (ValueError, DjangoValidationError):
                contribution = None
            if contribution is None or not contributions.Access(
                request.user
            ).is_delegate(contribution):
                raise
            return contribution.proposal_step.proposal

    def _locked_delegation_target(self, proposal, contribution_uuid):
        """The proposal locked, then the named contribution's step and the
        contribution, in the order of the step actions; the contribution's
        step comes attached. None for a contribution of another proposal."""
        locked_proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
            pk=proposal.pk
        )
        instance = self._locked_contribution_step(
            locked_proposal, None, contribution_uuid
        )
        if instance is None:
            return locked_proposal, None
        contribution = (
            models.StepContribution.objects.select_for_update(of=("self",))
            .filter(proposal_step=instance, uuid=contribution_uuid)
            .select_related("delegate", "delegated_by", "requested_offering")
            .first()
        )
        if contribution is not None:
            contribution.proposal_step = instance
        return locked_proposal, contribution

    @extend_schema(
        description=(
            "Hand a contribution slot to someone the step's delegable_to "
            "allows -- an offering manager of the slot's offering, or a holder "
            "of an evaluator role on the proposal -- or take it back with a "
            "null user_uuid. Its holders and the call's managers may; the "
            "person it is handed to accepts or declines it."
        ),
        request=serializers.DelegateStepContributionSerializer,
        responses={200: serializers.StepContributionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def delegate_contribution(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.DelegateStepContributionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        user_uuid = data["user_uuid"]
        with transaction.atomic():
            _, contribution = self._locked_delegation_target(
                proposal, data["contribution_uuid"]
            )
            access = contributions.Access(request.user)
            if contribution is None or not access.can_read(contribution):
                raise exceptions.NotFound()
            if not contributions.may_delegate(request.user, contribution, access):
                raise exceptions.PermissionDenied(
                    "You cannot hand this contribution on, or it is no longer open."
                )
            delegate_user = None
            if user_uuid is not None:
                # One answer for an unknown user and an ineligible one.
                delegate_user = User.objects.filter(uuid=user_uuid).first()
                if delegate_user is None:
                    raise exceptions.ValidationError(
                        {"user_uuid": ["This user cannot take the contribution over."]}
                    )
            try:
                previous = contributions.delegate(
                    contribution, request.user, delegate_user
                )
            except contributions.ContributionError as e:
                raise exceptions.ValidationError({"user_uuid": [str(e)]})
            uuid_hex = contribution.uuid.hex
            actor = request.user.uuid.hex
            if previous is not None:
                previous_uuid = previous.uuid.hex
                transaction.on_commit(
                    lambda: tasks.notify_contribution_delegated.delay(
                        uuid_hex, tasks.DelegationEvents.REVOKED, previous_uuid, actor
                    )
                )
            if delegate_user is not None:
                delegate_uuid = delegate_user.uuid.hex
                transaction.on_commit(
                    lambda: tasks.notify_contribution_delegated.delay(
                        uuid_hex, tasks.DelegationEvents.DELEGATED, delegate_uuid, actor
                    )
                )
        serializer = serializers.StepContributionSerializer(
            contribution, context={"request": request}
        )
        return response.Response(serializer.data)

    delegate_contribution_permissions = []
    delegate_contribution_serializer_class = (
        serializers.DelegateStepContributionSerializer
    )

    @extend_schema(
        description=(
            "Take over a contribution slot handed to the caller. To hand it "
            "back, decline it with decline_contribution."
        ),
        request=serializers.AcceptStepContributionSerializer,
        responses={200: serializers.StepContributionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def accept_contribution(self, request, uuid=None):
        proposal = self._proposal_for_delegate(request)
        input_serializer = serializers.AcceptStepContributionSerializer(
            data=request.data
        )
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        with transaction.atomic():
            _, contribution = self._locked_delegation_target(
                proposal, data["contribution_uuid"]
            )
            access = contributions.Access(request.user)
            if contribution is None or not access.is_delegate(contribution):
                raise exceptions.NotFound()
            try:
                contributions.accept_delegation(
                    contribution, request.user, data["confirm_no_conflict"]
                )
            except contributions.ContributionError as e:
                raise exceptions.ValidationError({"detail": str(e)})
            delegated_by = contribution.delegated_by
            if delegated_by is not None and delegated_by.pk != request.user.pk:
                uuid_hex = contribution.uuid.hex
                recipient = delegated_by.uuid.hex
                actor = request.user.uuid.hex
                transaction.on_commit(
                    lambda: tasks.notify_contribution_delegated.delay(
                        uuid_hex, tasks.DelegationEvents.ACCEPTED, recipient, actor
                    )
                )
        serializer = serializers.StepContributionSerializer(
            contribution, context={"request": request}
        )
        return response.Response(serializer.data)

    accept_contribution_permissions = []
    accept_contribution_serializer_class = serializers.AcceptStepContributionSerializer

    @extend_schema(
        description=(
            "What the proposal's per-offering assessments concluded about each "
            "requested offering -- accepted, conditionally accepted with its "
            "conditions, or rejected -- for the allocation decision."
        ),
        responses={200: serializers.OfferingAssessmentSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"])
    def offering_assessments(self, request, uuid=None):
        proposal = self.get_object()
        serializer = serializers.OfferingAssessmentSerializer(
            contributions.offering_assessments(
                request.user, proposal, access=contributions.access_for(request)
            ),
            many=True,
        )
        return response.Response(serializer.data)

    offering_assessments_permissions = []

    @extend_schema(
        summary="Get submitter dashboard stats",
        description=(
            "Returns counts of the current user's own proposals grouped by "
            "state, covering both in-progress and decided proposals."
        ),
        responses={200: serializers.DashboardSubmitterStatsSerializer},
    )
    @no_count_action
    @decorators.action(detail=False, methods=["get"], url_path="dashboard-stats")
    def dashboard_stats(self, request):
        payload = models.Proposal.objects.filter(created_by=request.user).aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(state=ProposalStates.DRAFT)),
            submitted=Count("id", filter=Q(state=ProposalStates.SUBMITTED)),
            in_review=Count("id", filter=Q(state=ProposalStates.IN_REVIEW)),
            accepted=Count("id", filter=Q(state=ProposalStates.ACCEPTED)),
            rejected=Count("id", filter=Q(state=ProposalStates.REJECTED)),
            canceled=Count("id", filter=Q(state=ProposalStates.CANCELED)),
        )
        serializer = serializers.DashboardSubmitterStatsSerializer(payload)
        return response.Response(serializer.data, status=status.HTTP_200_OK)


class ReviewViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalReviewSerializer
    filterset_class = filters.ReviewFilter
    queryset = models.Review.objects.all()

    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW)
    ]
    access_declarations = {
        "dashboard_stats": [access.owned("The caller's own reviews.")],
    }

    @access.shows(
        "Users with the staff or support flag, and holders of a site-wide "
        "CALL.LIST grant, see every review. Others see their own reviews, and "
        "the reviews of a call's proposals when they hold a role on the call's "
        "managing organisation, the call manager role on the call, or "
        "CALL.APPROVE_AND_REJECT_PROPOSALS on its organisation -- except on "
        "proposals they apply with, whose submitted reviews the applicant team "
        "sees once the proposal is decided, if the call shows reviews to "
        "submitters."
    )
    def get_queryset(self):
        # Support reads everything staff does and writes nothing; writes on
        # this viewset are gated separately, not by queryset visibility.
        return filter_reviews_for_user(self.request.user).order_by("created")

    def perform_create(self, serializer):
        proposal = serializer.validated_data["proposal"]
        if proposal.state not in [
            ProposalStates.DRAFT,
            ProposalStates.IN_REVIEW,
            ProposalStates.SUBMITTED,
        ]:
            raise exceptions.ValidationError(
                _("Valid states for proposals: Draft, In Review, Submitted.")
            )
        # Giving a review to someone is giving them review work.
        reviewer = serializer.validated_data["reviewer"]
        reviewer_standing.ensure_reviewer_eligible(reviewer, proposal.round.call)
        reviewer_standing.ensure_coi_disclosed(reviewer, proposal.round.call)
        with transaction.atomic():
            # A reviewer in the call's pool may not be given reviews above
            # their assignment limit; the pool entry is locked so concurrent
            # assignments are counted one at a time.
            pool_entry = (
                models.CallReviewerPool.objects.select_for_update(of=("self",))
                .filter(
                    call=proposal.round.call,
                    reviewer__user=serializer.validated_data["reviewer"],
                )
                .first()
            )
            if pool_entry:
                check_reviewer_workload_limit(
                    pool_entry,
                    1,
                    serializer.validated_data.get("override_workload_limit", False),
                    self.request.user,
                )
            review: models.Review = serializer.save()
            activity.log_review_event(review, EventType.REVIEW_ASSIGNED)
        tasks.notify_reviewer_about_assignment.delay(review.uuid)

    # Assigning or removing a review needs PROPOSAL.MANAGE_REVIEW on the call
    # or its managing organisation. The request, not the user, is checked so
    # that a personal access token's scopes and bindings apply.
    def check_create_permissions(request, view, obj=None):
        """Check permissions for creating reviews."""
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal = serializer.validated_data["proposal"]

        if not permissions_utils.has_permission_on_any_source(
            request,
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            proposal.round.call,
            CALL_PERMISSION_SOURCES,
        ):
            raise exceptions.PermissionDenied()

    def check_destroy_permissions(request, view, obj=None):
        """Check permissions for destroying reviews."""
        if obj and not permissions_utils.has_permission_on_any_source(
            request,
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            obj.proposal.round.call,
            CALL_PERMISSION_SOURCES,
        ):
            raise exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [check_destroy_permissions]

    @access.describes(
        access.any_of(access.owned("The review's reviewer."), access.staff()),
        needs_object=True,
    )
    def action_permission_check(request, view, obj: models.Review | None = None):
        """The review's reviewer, or a request acting as staff."""
        if not obj:
            return

        user = request.user

        if has_staff_access(request) or obj.reviewer == user:
            return

        raise exceptions.PermissionDenied()

    @extend_schema(
        description="Reject a review, changing its state to REJECTED.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        review: models.Review = self.get_object()
        review.state = models.Review.States.REJECTED
        review.rejection_cause = proposal_enums.ReviewRejectionCauses.DECLINED
        review.save()
        activity.log_review_event(review, EventType.REVIEW_REJECTED)
        tasks.notify_call_managers_about_rejected_review.delay(review.uuid)
        return response.Response(
            "Review has been rejected.",
            status=status.HTTP_200_OK,
        )

    reject_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW),
    ]

    @extend_schema(
        description="Submit a review, changing its state to SUBMITTED.",
        request=ReviewSubmitSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        review: models.Review = self.get_object()
        serializer = ReviewSubmitSerializer(
            instance=review, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(
            state=models.Review.States.SUBMITTED, submitted_at=timezone.now()
        )
        activity.log_review_event(review, EventType.REVIEW_SUBMITTED)
        tasks.notify_call_managers_about_new_review.delay(review.uuid)
        tasks.notify_manager_when_reviews_are_completed.delay(review.proposal.uuid)
        return response.Response(
            "Review has been submitted.",
            status=status.HTTP_200_OK,
        )

    @access.describes(needs_object=True)
    def reviewer_off_applicant_team(request, view, obj: models.Review | None = None):
        """Refused, to staff as well, when the review's reviewer is on the
        proposal's applicant team."""
        # Nobody reviews a proposal they are on the team of, however they
        # came to hold the review.
        if obj and proposal_roles.is_on_applicant_team(obj.reviewer, obj.proposal):
            raise exceptions.PermissionDenied(
                "A member of the applicant team cannot review the proposal."
            )

    submit_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW),
    ]
    reject_permissions = [action_permission_check]
    accept_permissions = submit_permissions = update_permissions = (
        partial_update_permissions
    ) = [action_permission_check, reviewer_off_applicant_team]

    @extend_schema(
        summary="Get reviewer dashboard stats and deadlines",
        description=(
            "Returns counts (assigned, pending, completed) for every review "
            "assigned to the current user, plus their nearest review "
            "deadlines ordered by due date. Overdue reviews sort first and "
            "are included; the counts cover all reviews, the deadline list is "
            "capped and deadlines_total gives its true length."
        ),
        responses={200: serializers.DashboardReviewerStatsSerializer},
    )
    @no_count_action
    @decorators.action(detail=False, methods=["get"], url_path="dashboard-stats")
    def dashboard_stats(self, request):
        user = request.user
        own_reviews = models.Review.objects.filter(reviewer=user)
        counts = own_reviews.aggregate(
            assigned=Count("id"),
            pending=Count("id", filter=Q(state=models.Review.States.IN_REVIEW)),
            completed=Count("id", filter=Q(state=models.Review.States.SUBMITTED)),
        )

        # A deadline only exists when the round sets a review duration;
        # computing it in SQL lets Postgres do the filtering and the ordering.
        reviews_with_deadline = (
            own_reviews.filter(state=models.Review.States.IN_REVIEW)
            .with_deadline()
            .select_related("proposal", "proposal__round", "proposal__round__call")
            .order_by("review_deadline")
        )
        # This list is embedded in an object, so it cannot be paginated the way
        # the standalone dashboard lists are. It carries its own total instead —
        # `pending` is not it, since a review whose round sets no review
        # duration has no deadline and never appears here.
        deadlines_total = reviews_with_deadline.count()
        deadlines = [
            {
                "uuid": review.uuid,
                "proposal_uuid": review.proposal.uuid,
                "proposal_name": review.proposal.name,
                "call_uuid": review.proposal.round.call.uuid,
                "call_name": review.proposal.round.call.name,
                "due_date": review.review_end_date,
            }
            for review in reviews_with_deadline[:DASHBOARD_LIST_LIMIT]
        ]

        payload = {
            **counts,
            "deadlines": deadlines,
            "deadlines_total": deadlines_total,
        }
        serializer = serializers.DashboardReviewerStatsSerializer(payload)
        return response.Response(serializer.data, status=status.HTTP_200_OK)


class ProviderRequestedOfferingViewSet(ReadOnlyActionsViewSet):
    """Requests to include the provider's offerings in calls.

    Scoped to the provider side: staff, the owners of the offering's
    organisation and whoever holds ``OFFERING.ACCEPT_CALL_REQUEST`` on the
    offering (``RequestedOffering.Permissions``). Each row says where its
    call stands and what the call's workflow asks of the offering's team."""

    lookup_field = "uuid"
    queryset = models.RequestedOffering.objects.filter().order_by(
        "offering", "call", "created"
    )
    serializer_class = serializers.ProviderRequestedOfferingSerializer
    filterset_class = filters.RequestedOfferingFilter
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    # Read the requested offering looked up, or the list filtered, as list
    # and retrieve do.
    access_filtered_actions = ("team_steps", "team", "team_step_slots", "team_slots")
    access_declarations = {
        "delegate_slots": [
            access.custom(
                "Whoever sees the requested offering. Each slot is handed on only "
                "if the caller reads it and may hand it on -- staff, the call's "
                "overseers or the slot's holder on an open slot of an active "
                "step (contributions.may_delegate); any other is skipped."
            )
        ],
    }

    def get_queryset(self):
        return provider_calls.annotate_requested_offerings(
            super()
            .get_queryset()
            .select_related(
                "call__manager__customer",
                "offering__customer",
                "offering__category",
                "plan",
                "created_by",
            )
            .prefetch_related("call__round_set", "offering__components")
        )

    @extend_schema(
        description=(
            "The steps of the call's workflow the offering's team acts in: "
            "those its offering managers complete, and those each requested "
            "offering's team contributes to. The call's other steps are not "
            "listed."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.OfferingTeamStepSerializer(many=True)
        },
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def team_steps(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        steps = provider_calls.team_steps(requested_offering.call)
        serializer = serializers.OfferingTeamStepSerializer(
            [serializers.OfferingTeamStepSerializer.describe(s) for s in steps],
            many=True,
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Accept a requested offering.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        requested_offering.state = RequestedOfferingStates.ACCEPTED
        requested_offering.approved_by = self.request.user
        requested_offering.save()
        tasks.notify_offering_request_decision.delay(requested_offering.uuid)
        return response.Response(
            "The request on offering has been accepted.",
            status=status.HTTP_200_OK,
        )

    accept_validators = [
        core_validators.StateValidator(RequestedOfferingStates.REQUESTED)
    ]

    @extend_schema(
        description=(
            "Decline the request: the offering does not take part in the call. "
            "The reason is told to the call's managers."
        ),
        request=serializers.RejectRequestedOfferingSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        serializer = serializers.RejectRequestedOfferingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_offering.state = RequestedOfferingStates.CANCELED
        requested_offering.approved_by = self.request.user
        requested_offering.rejection_reason = serializer.validated_data["reason"]
        requested_offering.save()
        tasks.notify_offering_request_decision.delay(requested_offering.uuid)
        return response.Response(
            "The request on offering has been canceled.",
            status=status.HTTP_200_OK,
        )

    cancel_validators = [
        core_validators.StateValidator(RequestedOfferingStates.REQUESTED)
    ]

    accept_permissions = cancel_permissions = [
        proposal_permissions.user_can_accept_requested_offering
    ]

    @extend_schema(
        description=(
            "The offering's team by name: its managers, who do the team's "
            "part of each step, and everyone else with a role on the offering, "
            "whom a slot may be handed to."
        ),
        request=None,
        responses={status.HTTP_200_OK: serializers.OfferingTeamSerializer},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def team(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        return response.Response(
            serializers.OfferingTeamSerializer(
                provider_calls.team_people(requested_offering.offering)
            ).data
        )

    @extend_schema(
        description=(
            "The team's open slots in the call: per proposal at a step the "
            "team has a part in, who holds it and when it is due."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.OfferingTeamSlotSerializer(many=True)
        },
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def team_step_slots(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        return response.Response(
            serializers.OfferingTeamSlotSerializer(
                provider_calls.open_team_slots(requested_offering), many=True
            ).data
        )

    @extend_schema(
        description=(
            "Hand several of the team's open slots to one person, or take them "
            "back with a null user_uuid. Each slot is handed on as "
            "delegate_contribution would; one that cannot be is left as it "
            "is and listed with why."
        ),
        request=serializers.DelegateTeamSlotsSerializer,
        responses={status.HTTP_200_OK: serializers.DelegateTeamSlotsResultSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def delegate_slots(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        serializer = serializers.DelegateTeamSlotsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_uuid = serializer.validated_data["user_uuid"]
        delegate_user = None
        if user_uuid is not None:
            delegate_user = User.objects.filter(uuid=user_uuid).first()
            if delegate_user is None:
                raise exceptions.ValidationError(
                    {"user_uuid": ["This user cannot take the slots over."]}
                )
        handed_on, skipped = [], {}
        access = contributions.Access(request.user)
        for contribution_uuid in serializer.validated_data["contribution_uuids"]:
            with transaction.atomic():
                contribution = (
                    models.StepContribution.objects.select_for_update(of=("self",))
                    .filter(
                        uuid=contribution_uuid, requested_offering=requested_offering
                    )
                    .select_related("proposal_step__call_step", "delegate")
                    .first()
                )
                if contribution is None or not access.can_read(contribution):
                    skipped[contribution_uuid.hex] = "No such slot of this offering."
                    continue
                if not contributions.may_delegate(request.user, contribution, access):
                    skipped[contribution_uuid.hex] = (
                        "You cannot hand this slot on, or it is no longer open."
                    )
                    continue
                try:
                    previous = contributions.delegate(
                        contribution, request.user, delegate_user
                    )
                except contributions.ContributionError as e:
                    skipped[contribution_uuid.hex] = str(e)
                    continue
                handed_on.append(contribution.uuid.hex)
                uuid_hex, actor = contribution.uuid.hex, request.user.uuid.hex
                if previous is not None:
                    previous_uuid = previous.uuid.hex
                    transaction.on_commit(
                        lambda u=uuid_hex, p=previous_uuid: (
                            tasks.notify_contribution_delegated.delay(
                                u, tasks.DelegationEvents.REVOKED, p, actor
                            )
                        )
                    )
                if delegate_user is not None:
                    delegate_uuid = delegate_user.uuid.hex
                    transaction.on_commit(
                        lambda u=uuid_hex: tasks.notify_contribution_delegated.delay(
                            u, tasks.DelegationEvents.DELEGATED, delegate_uuid, actor
                        )
                    )
        return response.Response({"handed_on": handed_on, "skipped": skipped})

    delegate_slots_serializer_class = serializers.DelegateTeamSlotsSerializer

    @extend_schema(
        description=(
            "The open slots of the teams of every offering the caller sees "
            "here -- their colleagues' and delegates' as well as their own -- "
            "across the calls. Filter as the list is filtered (provider_uuid, "
            "offering_uuid, call_uuid)."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.OfferingTeamSlotSerializer(many=True)
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def team_slots(self, request):
        rows = []
        for requested_offering in self.filter_queryset(self.get_queryset()).filter(
            state=RequestedOfferingStates.ACCEPTED
        ):
            rows.extend(provider_calls.open_team_slots(requested_offering))
        rows.sort(key=lambda row: (row["due_at"] is None, row["due_at"] or 0))
        return response.Response(
            serializers.OfferingTeamSlotSerializer(rows, many=True).data
        )

    @extend_schema(
        description=(
            "Name who the offering's slots are handed to as they open, on "
            "steps whose slots may be handed within the team, or nobody with "
            "a null user_uuid. Slots already open stay as they are."
        ),
        request=serializers.SetDefaultDelegateSerializer,
        responses={status.HTTP_200_OK: serializers.SetDefaultDelegateSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_default_delegate(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        serializer = serializers.SetDefaultDelegateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_uuid = serializer.validated_data["user_uuid"]
        user = None
        if user_uuid is not None:
            user = User.objects.filter(
                uuid=user_uuid,
                is_active=True,
                id__in=contributions.offering_role_holder_ids(
                    requested_offering.offering_id
                ),
            ).first()
            if user is None:
                raise exceptions.ValidationError(
                    {"user_uuid": ["Only someone with a role on the offering."]}
                )
        requested_offering.default_delegate = user
        requested_offering.save(update_fields=["default_delegate"])
        return response.Response({"user_uuid": user.uuid.hex if user else None})

    set_default_delegate_serializer_class = serializers.SetDefaultDelegateSerializer
    set_default_delegate_permissions = [
        proposal_permissions.user_can_accept_requested_offering
    ]


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                "include_closed",
                OpenApiTypes.BOOL,
                description=(
                    "Include requests belonging to rejected or canceled "
                    "proposals. They are omitted by default."
                ),
            )
        ]
    )
)
class UserRequestedResourceViewSet(ReadOnlyActionsViewSet):
    """Resources the current user has requested through a proposal.

    Counterpart of ``ProviderRequestedResourceViewSet``, which scopes to
    offerings the user *manages* — the service provider's view of who applied.
    This one scopes to proposals the user can read, reusing the very rule
    ``ProposalViewSet`` applies, so this list and "My proposals" can never
    disagree about what the user is party to.

    Note it does not scope through ``RequestedResource.Permissions.project_path``
    (``proposal__project``): that project does not exist until the proposal is
    approved, so it would hide exactly the pending rows this page is about.

    Requests on rejected or canceled proposals are left out unless asked for:
    they are settled questions, and listing them by default buries the requests
    the user can still act on. ``?include_closed=true`` brings them back.
    """

    lookup_field = "uuid"
    serializer_class = serializers.UserRequestedResourceSerializer
    filterset_class = filters.RequestedResourceFilter
    filter_backends = (DjangoFilterBackend,)

    @access.shows(
        "Requested resources of the proposals the caller can read, as the "
        "proposal list shows them; those of rejected or canceled proposals only "
        "with include_closed=true. Unless the caller has the staff or support "
        "flag, a proposal read only as one of its providers shows only the "
        "requests for offerings the caller holds a role on."
    )
    def get_queryset(self):
        proposals = filter_queryset_for_user(
            models.Proposal.objects.all(), self.request.user
        )
        if self.request.query_params.get("include_closed") not in ("true", "True"):
            proposals = proposals.exclude(
                state__in=[ProposalStates.CANCELED, ProposalStates.REJECTED]
            )
        rows = models.RequestedResource.objects.filter(proposal__in=proposals)
        user = self.request.user
        if user.is_authenticated and not (user.is_staff or user.is_support):
            # A proposal read only as one of its providers shows only their
            # own offerings' requests (``permissions.offering_scope``).
            rows = rows.filter(
                Q(proposal__in=proposal_permissions.proposals_read_whole(user))
                | Q(
                    requested_offering__offering_id__in=(
                        contributions.offering_ids_with_role_of(user)
                    )
                )
            )
        return rows.select_related(
            "proposal",
            "proposal__round__call",
            "requested_offering__offering",
            "resource",
        ).order_by("-created")


class ProviderRequestedResourceViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProviderRequestedResourceSerializer
    filterset_class = filters.RequestedResourceFilter
    filter_backends = (DjangoFilterBackend,)

    @access.shows(
        "Users with the staff flag see every requested resource. Others see "
        "the requests for the offerings they can manage -- through a role on "
        "the offering, its organisation, service provider or project, a "
        "site-wide OFFERING.LIST grant, or the support flag -- except those of "
        "draft proposals."
    )
    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.RequestedResource.objects.all().order_by(
                "resource", "proposal", "created"
            )

        offerings_ids = (
            marketplace_models.Offering.objects.all()
            .filter_for_user(user)
            .values_list("id", flat=True)
        )
        # An applicant's draft is not an application yet: the provider sees a
        # proposal once it is submitted.
        return (
            models.RequestedResource.objects.filter(
                requested_offering__offering_id__in=offerings_ids
            )
            .exclude(proposal__state__in=provider_calls.SHOWN_PROPOSAL_EXCLUDED_STATES)
            .select_related(
                "proposal__round__call__manager__customer",
                "requested_offering__offering__customer",
                "requested_offering__plan",
                "resource",
                "created_by",
            )
            .order_by("resource", "proposal", "created")
        )


class OfferingDemandViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """How much the proposals of calls ask of the provider's offerings.

    One row per offering, call, round and group of proposal states --
    pending (submitted or in review), accepted, closed (rejected or
    cancelled) -- with the proposals counted and, per component, the
    requested amounts summed and, for accepted proposals, the awarded ones.
    Drafts are left out. Where the call pools a component, the pool's
    figures for the whole call and for the round come with it.

    Only the provider side reads it: staff and support every offering, anyone
    else the offerings of the organisations they own and those they hold an
    offering role on. The rows are an aggregate and are not paginated.
    """

    serializer_class = serializers.OfferingDemandSerializer
    permission_classes = (rf_permissions.IsAuthenticated,)
    filter_backends = ()
    pagination_class = None

    UUID_FILTERS = ("offering_uuid", "call_uuid", "round_uuid", "provider_uuid")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "offering_uuid",
                OpenApiTypes.UUID,
                description="Only this offering.",
                extensions={
                    "x-waldur-operation-id": "marketplace_provider_offerings_retrieve"
                },
            ),
            OpenApiParameter(
                "call_uuid",
                OpenApiTypes.UUID,
                description="Only this call.",
                extensions={"x-waldur-operation-id": "proposal_public_calls_retrieve"},
            ),
            OpenApiParameter(
                "round_uuid",
                OpenApiTypes.UUID,
                description="Only this round of a call.",
                extensions={"x-waldur-operation-id": "call_rounds_retrieve"},
            ),
            OpenApiParameter(
                "provider_uuid",
                OpenApiTypes.UUID,
                description="Only the offerings of this provider organization.",
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
            OpenApiParameter(
                "state_group",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                many=True,
                enum=list(offering_demand.DemandGroups.ORDER),
                description="Only these groups of proposal states.",
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @access.shows(
        "Demand for the offerings of the organisations the caller owns and of "
        "the offerings they hold a role on; users with the staff or support "
        "flag see every offering's."
    )
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return []
        params = self.request.query_params
        uuids = {}
        for name in self.UUID_FILTERS:
            value = params.get(name)
            if not value:
                continue
            try:
                uuids[name] = uuid_module.UUID(value).hex
            except ValueError:
                raise exceptions.ValidationError({name: _("Not a valid UUID.")})
        groups = params.getlist("state_group")
        unknown = [
            group for group in groups if group not in offering_demand.DemandGroups.ORDER
        ]
        if unknown:
            raise exceptions.ValidationError(
                {"state_group": _("Unknown state group: %s.") % ", ".join(unknown)}
            )
        return offering_demand.demand(self.request.user, groups=groups, **uuids)


class RoundViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.CallRoundSerializer
    filterset_class = []
    filter_backends = (DjangoFilterBackend,)
    # Looks the round up as retrieve does and lists who reviews in it.
    access_filtered_actions = ("reviewers",)
    access_declarations = {
        "dashboard_deadlines": [
            access.owned(
                "Open rounds of the calls on which the caller holds the call "
                "manager role."
            )
        ],
    }

    @access.shows(
        "Users with the staff or support flag, and holders of a site-wide "
        "ROUND.LIST grant, see every round. Others see the rounds of calls "
        "whose managing organisation they hold a role on, or whose "
        "organisation grants them ROUND.LIST."
    )
    def get_queryset(self):
        return filter_queryset_for_user(models.Round.objects.all(), self.request.user)

    @extend_schema(
        description="Return list of reviewers for round.",
        request=None,
        responses=serializers.RoundReviewerSerializer(many=True),
    )
    @decorators.action(detail=True)
    def reviewers(self, request, uuid=None):
        round_obj = self.get_object()

        unique_reviewer_ids = (
            models.Review.objects.filter(proposal__round=round_obj)
            .values_list("reviewer", flat=True)
            .distinct()
        )
        users = User.objects.filter(id__in=unique_reviewer_ids)

        proposals = models.Proposal.objects.filter(
            review__reviewer=OuterRef("pk"), round=round_obj
        )

        accepted_proposals_subquery = proposals.filter(
            state=ProposalStates.ACCEPTED
        ).values("pk")

        rejected_proposals_subquery = proposals.filter(
            state=ProposalStates.REJECTED
        ).values("pk")

        in_review_proposals_subquery = proposals.filter(
            state=ProposalStates.IN_REVIEW
        ).values("pk")

        users = users.annotate(
            accepted_proposals=Coalesce(SubqueryCount(accepted_proposals_subquery), 0),
            rejected_proposals=Coalesce(SubqueryCount(rejected_proposals_subquery), 0),
            in_review_proposals=Coalesce(
                SubqueryCount(in_review_proposals_subquery), 0
            ),
        )

        page = self.paginate_queryset(users)
        serializer = serializers.RoundReviewerSerializer(
            page, many=True, context={"round_obj": round_obj}
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="Get upcoming round deadlines for the call manager dashboard",
        description=(
            "Returns the rounds in calls managed by the current user that are "
            "still open (cutoff time in the future), nearest first. Rounds "
            "without a cut-off come last, with no due date. Paginated: the "
            "exact total is in the X-Result-Count header."
        ),
        responses={200: serializers.DashboardUpcomingDeadlineSerializer(many=True)},
    )
    @decorators.action(detail=False, methods=["get"], url_path="dashboard-deadlines")
    def dashboard_deadlines(self, request):
        user = request.user
        managed_call_ids = get_connected_calls(user, CallRole.MANAGER)
        rounds = (
            models.Round.objects.filter(round_not_ended_q())
            .filter(call_id__in=managed_call_ids)
            .select_related("call")
            .order_by(F("cutoff_time").asc(nulls_last=True), "start_time", "id")
        )
        # Paginated rather than sliced to DASHBOARD_LIST_LIMIT: PAGE_SIZE is
        # also 10, so the page the dashboard renders is unchanged, but a manager
        # running more than ten open rounds no longer sees the first ten
        # presented as all of them.
        page = self.paginate_queryset(rounds)
        if request.method == "HEAD":
            # Count-only request (the `_count` companion): the X-Result-Count
            # header is set from the paginator, so skip serialising the page.
            return self.get_paginated_response([])
        payload = [
            {
                "uuid": round_obj.uuid,
                "call_uuid": round_obj.call.uuid,
                "call_name": round_obj.call.name,
                "round_name": round_obj.name,
                "due_date": round_obj.cutoff_time,
            }
            for round_obj in page
        ]
        serializer = serializers.DashboardUpcomingDeadlineSerializer(payload, many=True)
        return self.get_paginated_response(serializer.data)


class ProposalProjectRoleMappingViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalProjectRoleMappingSerializer
    queryset = models.ProposalProjectRoleMapping.objects.all().order_by("call")
    filterset_class = filters.ProposalProjectRoleMappingFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [proposal_permissions.CanUpdateCallPermission]
    # Archived calls are read-only. Update/delete run against the mapping
    # (which reaches its call via ``.call``); create is guarded in the
    # serializer's ``validate`` where the target call is known.
    update_validators = partial_update_validators = destroy_validators = [
        validate_call_not_archived
    ]
    access_declarations = {
        "create": [
            access.custom(
                "The serializer requires CALL.UPDATE on the call named in the "
                "request or on its managing organisation."
            )
        ],
    }

    @access.shows(
        "Users with the staff or support flag see every mapping; others the "
        "mappings of the calls they can see."
    )
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return queryset
        # A mapping is visible exactly when its call is. Writes stay gated on
        # UPDATE_CALL.
        return queryset.filter(
            call__in=filter_queryset_for_user(models.Call.objects.all(), user)
        )

    def perform_create(self, serializer):
        mapping = serializer.save()
        workflow_versions.record_on_commit(mapping.call, self.request.user)

    def perform_update(self, serializer):
        mapping = serializer.save()
        workflow_versions.record_on_commit(mapping.call, self.request.user)

    def perform_destroy(self, instance):
        call = instance.call
        super().perform_destroy(instance)
        workflow_versions.record_on_commit(call, self.request.user)

    def get_permissions(self):
        # CanUpdateCallPermission implements only has_object_permission, which
        # DRF never calls for a list route -- without this the collection was
        # readable anonymously. Reads are scoped by get_queryset instead.
        if self.action in ("list", "retrieve"):
            return [rf_permissions.IsAuthenticated()]
        return super().get_permissions()


class CallWorkflowStepNotificationRuleViewSet(ActionsViewSet):
    """Call-level notification rules attached to workflow steps.

    Readable by anyone connected to the call (managers, reviewers, panel), so
    the review UI can show who is notified; writable by call managers only
    (``CanUpdateCallPermission`` on update/delete, the serializer on create).
    Archived calls are read-only.
    """

    lookup_field = "uuid"
    serializer_class = serializers.CallWorkflowStepNotificationRuleSerializer
    queryset = models.CallWorkflowStepNotificationRule.objects.all().select_related(
        "workflow_step", "workflow_step__call"
    )
    filterset_class = filters.CallWorkflowStepNotificationRuleFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [proposal_permissions.CanUpdateCallPermission]
    update_validators = partial_update_validators = destroy_validators = [
        validate_call_not_archived
    ]

    access_declarations = {
        "create": [
            access.custom(
                "The serializer requires CALL.UPDATE on the call of the workflow "
                "step named in the request or on its managing organisation."
            )
        ],
    }

    @access.shows(
        "Users with the staff or support flag, and holders of a site-wide "
        "CALL.LIST grant, see every rule; others the rules of the calls they "
        "hold a role on."
    )
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support or sees_every_call(user):
            return queryset
        return queryset.filter(workflow_step__call__in=get_connected_calls(user))

    def perform_create(self, serializer):
        rule = serializer.save()
        workflow_versions.record_on_commit(rule.workflow_step.call, self.request.user)

    def perform_update(self, serializer):
        rule = serializer.save()
        workflow_versions.record_on_commit(rule.workflow_step.call, self.request.user)

    def perform_destroy(self, instance):
        call = instance.workflow_step.call
        super().perform_destroy(instance)
        workflow_versions.record_on_commit(call, self.request.user)

    def get_permissions(self):
        # Object-level UPDATE_CALL is only meaningful for writes; reads are
        # scoped by get_queryset.
        if self.action in ("list", "retrieve"):
            return [rf_permissions.IsAuthenticated()]
        return super().get_permissions()


class ProposalRoleAssignmentViewSet(ReadOnlyActionsViewSet):
    """Evaluator roles held on proposals, from the holder's side.

    A holder sees their own assignments -- including those still waiting for
    them to confirm having no conflict of interest, which give no access to
    the proposal yet -- and confirms them here. Those overseeing a call see
    its assignments too; staff and support see all.
    """

    lookup_field = "uuid"
    serializer_class = serializers.ProposalRoleAssignmentSerializer
    filterset_class = filters.ProposalRoleAssignmentFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = (rf_permissions.IsAuthenticated,)
    access_declarations = {
        "confirm_no_conflict": [
            access.owned("The role assignment's holder, checked in the action.")
        ],
    }

    @access.shows(
        "Users with the staff or support flag see every assignment. Others see "
        "their own, and those on proposals of calls they hold the call manager "
        "role on or whose managing organisation they hold a role on, except "
        "proposals they apply with."
    )
    def get_queryset(self):
        user = self.request.user
        if getattr(self, "swagger_fake_view", False):
            return proposal_roles.all_assignments().none()
        queryset = (
            proposal_roles.with_standing_conflict(proposal_roles.all_assignments())
            .select_related(
                "role",
                "user",
                "assigned_by",
                "revoked_by",
                "assigned_in__call_step",
                "proposal__round__call",
            )
            .order_by("-created")
        )
        if user.is_staff or user.is_support:
            return queryset
        own = proposal_roles.assignments_of(user).values_list("pk", flat=True)
        managed = Q(
            proposal__round__call__in=get_connected_calls(user, CallRole.MANAGER)
        ) | Q(proposal__round__call__manager__in=get_connected_call_organizers(user))
        # Managing the call does not show who evaluates one's own proposal.
        applicant = Q(proposal_id__in=proposal_roles.applicant_team_proposal_ids(user))
        return queryset.filter(Q(pk__in=own) | (managed & ~applicant))

    def get_serializer_context(self):
        # Only the holders themselves and those overseeing the call see a
        # row at all, so each sees it in full.
        return {**super().get_serializer_context(), "full_detail": True}

    @extend_schema(
        request=None,
        responses=serializers.ProposalRoleAssignmentSerializer,
        description=(
            "Confirm having no conflict of interest with the proposal, as the "
            "holder of the role. Required by some calls before the proposal "
            "opens to its evaluators; refused while a conflict is on record."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def confirm_no_conflict(self, request, uuid=None):
        assignment = self.get_object()
        if assignment.user_id != request.user.id:
            raise exceptions.PermissionDenied(
                _("Only the holder of the role can confirm this.")
            )
        with transaction.atomic():
            # The proposal first, as for every change to its slots.
            models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=assignment.proposal_id
            )
            assignment = proposal_roles.confirm_no_conflict(assignment)
            contributions.sync_lead_slots(assignment.proposal, assignment.role)
            completed, next_instance = workflow_service.settle_evaluator_slots(
                assignment.proposal, assignment.user, request.user
            )
        tasks.notify_contribution_decision(
            assignment.proposal, completed, next_instance
        )
        return response.Response(
            self.get_serializer(assignment).data,
            status=status.HTTP_200_OK,
        )


# =============================================================================
# Reviewer Profile ViewSets
# =============================================================================


class ExpertiseCategoryViewSet(ReadOnlyActionsViewSet):
    """Read-only ViewSet for expertise categories (taxonomy)."""

    lookup_field = "uuid"
    queryset = models.ExpertiseCategory.objects.all().order_by("code")
    serializer_class = serializers.ExpertiseCategorySerializer
    filterset_class = filters.ExpertiseCategoryFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = (rf_permissions.IsAuthenticated,)
    # A taxonomy every reviewer picks their expertise from.
    access_declarations = {
        "list": [access.authenticated()],
        "retrieve": [access.authenticated()],
    }


# ActionsPermission also calls these checks without the object, where they
# refuse everyone but staff: the owner edits their profile through ``me``.
@access.describes(access.staff())
def check_reviewer_profile_update_permission(request, view, obj=None):
    """A request acting as staff. Meant to admit the profile's owner too, but
    the check also runs without the object, where it refuses everyone else."""
    if has_staff_access(request):
        return
    if obj and obj.user == request.user:
        return
    raise exceptions.PermissionDenied(
        _("You do not have permission to update this reviewer profile.")
    )


@access.describes(access.staff())
def check_reviewer_profile_delete_permission(request, view, obj=None):
    """A request acting as staff, through
    ``check_reviewer_profile_update_permission``. Meant to let the owner
    delete their profile only while in good standing: a suspended or blocked
    reviewer would otherwise delete it, with its conflict of interest
    history, and start afresh."""
    check_reviewer_profile_update_permission(request, view, obj)
    if (
        obj
        and not request.user.is_staff
        and obj.standing != proposal_enums.ReviewerStandings.ACTIVE
    ):
        raise exceptions.PermissionDenied(
            _(
                "A reviewer profile that is suspended or blocked cannot be "
                "deleted. Contact the platform operator."
            )
        )


class ReviewerProfileViewSet(ActionsViewSet):
    """ViewSet for managing reviewer profiles."""

    lookup_field = "uuid"
    queryset = models.ReviewerProfile.objects.all().order_by(
        "user__first_name", "user__last_name"
    )
    serializer_class = serializers.ReviewerProfileSerializer
    filterset_class = filters.ReviewerProfileFilter
    filter_backends = (DjangoFilterBackend,)
    update_permissions = partial_update_permissions = [
        check_reviewer_profile_update_permission
    ]
    destroy_permissions = [check_reviewer_profile_delete_permission]
    # The ORCID actions look the profile up as retrieve does, then check its
    # owner in the action body.
    _OWNER_OR_STAFF = [
        access.any_of(access.owned("The profile's owner."), access.staff())
    ]
    access_declarations = {
        "create": [access.owned("A reviewer profile for the caller themselves.")],
        "me": [access.owned("The caller's own reviewer profile.")],
        "publish": [access.owned("The caller's own reviewer profile.")],
        "unpublish": [access.owned("The caller's own reviewer profile.")],
        "connect_orcid": _OWNER_OR_STAFF,
        "connect_orcid_callback": _OWNER_OR_STAFF,
        "sync_orcid": _OWNER_OR_STAFF,
        "disconnect_orcid": _OWNER_OR_STAFF,
        "import_publications": _OWNER_OR_STAFF,
    }

    @access.shows(
        "Users with the staff flag see every profile. Others see their own, "
        "and those of reviewers who accepted a place in the pool of a call they "
        "hold the call manager role on -- of any call with the support flag or "
        "a site-wide CALL.LIST grant."
    )
    def get_queryset(self):
        # The reviewer's record, worked out from their reviews for the whole
        # page in one query.
        return reviewer_stats.annotate(self._visible_profiles())

    def _visible_profiles(self):
        user = self.request.user
        if user.is_staff:
            return models.ReviewerProfile.objects.all().order_by(
                "user__first_name", "user__last_name"
            )
        # Users can see their own profile and profiles of ACCEPTED pool members
        # (pending invitations don't expose profiles to managers). Support sees
        # the accepted members of every call's pool -- what the call pages they
        # can open show -- but not unpooled or pending profiles as staff does.
        accepted = Q(
            pool_memberships__invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED
        )
        if user.is_support or sees_every_call(user):
            pooled = accepted
        else:
            pooled = accepted & Q(
                pool_memberships__call__in=get_connected_calls(user, CallRole.MANAGER)
            )
        return (
            models.ReviewerProfile.objects.filter(
                Q(user=user)  # Own profile always visible
                | pooled
            )
            .distinct()
            .order_by("user__first_name", "user__last_name")
        )

    @extend_schema(
        description="Get or create reviewer profile for the current user.",
        request=serializers.ReviewerProfileCreateSerializer,
        responses=serializers.ReviewerProfileSerializer,
    )
    @decorators.action(detail=False, methods=["get", "post", "patch"])
    def me(self, request):
        """Get or create reviewer profile for the current user."""
        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            if request.method == "GET":
                return response.Response(
                    {"detail": "Reviewer profile not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            profile = None

        if request.method == "GET":
            serializer = self.get_serializer(profile)
            return response.Response(serializer.data)
        elif request.method == "POST" and profile is None:
            reviewer_standing.ensure_reviewer_profiles_enabled()
            serializer = serializers.ReviewerProfileCreateSerializer(
                data=request.data, context=self.get_serializer_context()
            )
            serializer.is_valid(raise_exception=True)
            profile = serializer.save()
            return response.Response(
                self.get_serializer(profile).data,
                status=status.HTTP_201_CREATED,
            )
        elif request.method in ["POST", "PATCH"] and profile is not None:
            serializer = serializers.ReviewerProfileSerializer(
                profile,
                data=request.data,
                partial=True,
                context=self.get_serializer_context(),
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return response.Response(serializer.data)
        else:
            return response.Response(
                {"detail": "Profile already exists. Use PATCH to update."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Note: Nested affiliations, expertise, and publications management
    # are handled by dedicated ViewSets (ReviewerProfileAffiliationViewSet,
    # ReviewerProfileExpertiseViewSet, ReviewerProfilePublicationViewSet)
    # registered in waldur_core/server/urls.py using NestedSimpleRouter.
    # These provide full CRUD operations at endpoints like:
    # /api/reviewer-profiles/{uuid}/affiliations/
    # /api/reviewer-profiles/{uuid}/expertise/
    # /api/reviewer-profiles/{uuid}/publications/

    # ORCID Integration
    @extend_schema(
        description="Get ORCID OAuth authorization URL.",
        responses={
            200: {
                "type": "object",
                "properties": {"authorization_url": {"type": "string"}},
            }
        },
    )
    @decorators.action(detail=True, methods=["get"], url_path="connect-orcid")
    def connect_orcid(self, request, uuid=None):
        """Get ORCID OAuth authorization URL to initiate connection."""
        profile = self.get_object()

        # Check if user owns this profile
        if profile.user != request.user and not has_staff_access(request):
            raise exceptions.PermissionDenied(
                _("You can only connect ORCID to your own profile.")
            )

        if not orcid_service.is_orcid_configured():
            raise exceptions.ValidationError(_("ORCID integration is not configured."))

        # Generate state token for CSRF protection
        state = secrets.token_urlsafe(32)
        # Store state in session or cache for validation
        request.session[f"orcid_state_{profile.uuid}"] = state

        auth_url = orcid_service.get_authorization_url(state=state)
        return response.Response({"authorization_url": auth_url})

    @extend_schema(
        description="Complete ORCID OAuth connection with authorization code.",
        request=serializers.OrcidCallbackSerializer,
        responses=serializers.ReviewerProfileSerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="connect-orcid/callback")
    def connect_orcid_callback(self, request, uuid=None):
        """Complete ORCID OAuth flow with authorization code."""
        profile = self.get_object()

        if profile.user != request.user and not has_staff_access(request):
            raise exceptions.PermissionDenied(
                _("You can only connect ORCID to your own profile.")
            )

        code = request.data.get("code")
        if not code:
            raise exceptions.ValidationError(
                {"code": _("Authorization code is required.")}
            )

        try:
            token_data = orcid_service.exchange_code_for_token(code)
        except orcid_service.ORCIDAuthError as e:
            raise exceptions.ValidationError({"code": str(e)})

        # Update profile with ORCID data
        profile.orcid_id = token_data.get("orcid")
        profile.orcid_access_token = token_data.get("access_token", "")
        profile.orcid_refresh_token = token_data.get("refresh_token", "")
        profile.orcid_last_sync = timezone.now()
        profile.save()

        return response.Response(self.get_serializer(profile).data)

    @extend_schema(
        description="Sync profile data from ORCID.",
        responses={200: serializers.OrcidSyncResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="sync-orcid")
    def sync_orcid(self, request, uuid=None):
        """Sync profile data from connected ORCID account."""
        profile = self.get_object()

        if profile.user != request.user and not has_staff_access(request):
            raise exceptions.PermissionDenied(
                _("You can only sync your own ORCID profile.")
            )

        if not profile.orcid_id:
            raise exceptions.ValidationError(
                _("ORCID is not connected. Please connect ORCID first.")
            )

        try:
            # Import publications
            pub_result = orcid_service.import_orcid_works(profile)
            # Import affiliations
            affil_result = orcid_service.import_orcid_affiliations(profile)
            # Import keywords as expertise
            keyword_result = orcid_service.import_orcid_keywords(profile)

            profile.orcid_last_sync = timezone.now()
            profile.save(update_fields=["orcid_last_sync"])

            return response.Response(
                {
                    "imported": {
                        "publications": pub_result,
                        "affiliations": affil_result,
                        "keywords": keyword_result,
                    },
                    "last_sync": profile.orcid_last_sync,
                }
            )
        except orcid_service.ORCIDAPIError as e:
            raise exceptions.ValidationError({"detail": str(e)})

    @extend_schema(
        description="Disconnect ORCID from profile.",
        responses={200: serializers.OrcidDisconnectResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="disconnect-orcid")
    def disconnect_orcid(self, request, uuid=None):
        """Disconnect ORCID from profile."""
        profile = self.get_object()

        if profile.user != request.user and not has_staff_access(request):
            raise exceptions.PermissionDenied(
                _("You can only disconnect your own ORCID.")
            )

        profile.orcid_id = ""
        profile.orcid_access_token = ""
        profile.orcid_refresh_token = ""
        profile.orcid_last_sync = None
        profile.save(
            update_fields=[
                "orcid_id",
                "orcid_access_token",
                "orcid_refresh_token",
                "orcid_last_sync",
            ]
        )

        return response.Response({"detail": _("ORCID disconnected successfully.")})

    @extend_schema(
        description="Import publications from ORCID or other sources.",
        request=serializers.ImportPublicationsSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {"imported_count": {"type": "integer"}},
            }
        },
    )
    @decorators.action(detail=True, methods=["post"], url_path="import-publications")
    def import_publications(self, request, uuid=None):
        """Import publications from various sources."""
        profile = self.get_object()

        if profile.user != request.user and not has_staff_access(request):
            raise exceptions.PermissionDenied(
                _("You can only import publications to your own profile.")
            )

        source = request.data.get("source", "orcid")

        if source == "orcid":
            if not profile.orcid_id:
                raise exceptions.ValidationError(
                    _("ORCID is not connected. Please connect ORCID first.")
                )

            result = orcid_service.import_orcid_works(profile)
            return response.Response({"imported_count": result.get("created", 0)})

        elif source == "doi":
            doi = request.data.get("doi")
            if not doi:
                raise exceptions.ValidationError({"doi": _("DOI is required.")})

            try:
                pub_data = orcid_service.fetch_publication_by_doi(doi)
                if pub_data:
                    pub, created = models.ReviewerPublication.objects.get_or_create(
                        reviewer_profile=profile,
                        doi=doi,
                        defaults=pub_data,
                    )
                    return response.Response(
                        {
                            "imported_count": 1 if created else 0,
                            "publication": serializers.ReviewerPublicationSerializer(
                                pub
                            ).data,
                        }
                    )
                else:
                    raise exceptions.ValidationError(
                        {"doi": _("Could not find publication with this DOI.")}
                    )
            except Exception as e:
                raise exceptions.ValidationError({"detail": str(e)})

        else:
            raise exceptions.ValidationError(
                {"source": _("Invalid source. Use 'orcid' or 'doi'.")}
            )

    # Profile visibility management
    @extend_schema(
        description=(
            "Publish reviewer profile for discovery by call managers. "
            "Warning: Publishing makes your full profile visible to call managers globally."
        ),
        responses={
            200: {
                "type": "object",
                "properties": {
                    "is_published": {"type": "boolean"},
                    "published_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                    },
                    "publication_pending": {"type": "boolean"},
                    "detail": {"type": "string"},
                    "warning": {"type": "string"},
                },
            }
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def publish(self, request):
        """Publish reviewer profile for discovery, or ask the operator to."""
        reviewer_standing.ensure_reviewer_profiles_enabled()
        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must create a reviewer profile first.")
            )

        if profile.is_published:
            return response.Response(
                {
                    "is_published": True,
                    "published_at": profile.published_at,
                    "publication_pending": False,
                    "detail": _("Profile is already published."),
                }
            )

        pending = reviewer_registry.publish(profile, request.user)
        if pending is not None:
            return response.Response(
                {
                    "is_published": False,
                    "published_at": None,
                    "publication_pending": True,
                    "detail": _(
                        "Your profile will be discoverable once the platform "
                        "operator approves its publication."
                    ),
                }
            )
        return response.Response(
            {
                "is_published": True,
                "published_at": profile.published_at,
                "publication_pending": False,
                "warning": _(
                    "Your full reviewer profile is now discoverable by call managers globally. "
                    "This includes your biography, publications, affiliations, and expertise areas."
                ),
            }
        )

    @extend_schema(
        description="Unpublish reviewer profile to remove it from discovery.",
        responses={
            200: {
                "type": "object",
                "properties": {
                    "is_published": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
            }
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def unpublish(self, request):
        """Unpublish reviewer profile to hide it from discovery."""
        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must create a reviewer profile first.")
            )

        if not profile.is_published:
            # Unpublishing withdraws a request still awaiting approval.
            withdrawn = reviewer_registry.withdraw_publication_request(profile)
            return response.Response(
                {
                    "is_published": False,
                    "detail": _("Your publication request is withdrawn.")
                    if withdrawn
                    else _("Profile is already unpublished."),
                }
            )

        profile.is_published = False
        profile.save(update_fields=["is_published"])

        return response.Response(
            {
                "is_published": False,
                "detail": _(
                    "Your profile is no longer discoverable. "
                    "Existing invitations and pool memberships are not affected."
                ),
            }
        )


class ReviewerRegistryViewSet(viewsets.ReadOnlyModelViewSet):
    """The platform operator's reviewer registry (staff only).

    Every reviewer with their standing, publication state, the calls they
    review for and the review work they hold; staff set a reviewer's standing
    and decide publication requests here. Reviewers and call managers have no
    access: a reviewer reads their own standing on their profile, a call
    manager on the pool.
    """

    lookup_field = "uuid"
    serializer_class = serializers.ReviewerRegistrySerializer
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ReviewerRegistryFilter

    def get_queryset(self):
        open_items = models.AssignmentItem.objects.filter(
            open_assignment_items_q(),
            batch__reviewer_pool_entry__reviewer=OuterRef("pk"),
        ).values("pk")
        direct_reviews = models.Review.objects.filter(
            reviewer=OuterRef("user_id"),
            state=models.Review.States.IN_REVIEW,
            assignment_item__isnull=True,
        ).values("pk")
        return (
            models.ReviewerProfile.objects.select_related("user", "standing_changed_by")
            .prefetch_related(
                "publication_requests",
                Prefetch(
                    "pool_memberships",
                    queryset=models.CallReviewerPool.objects.filter(
                        invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED
                    ).select_related("call"),
                    to_attr="accepted_pools",
                ),
            )
            .annotate(
                open_assignments=SubqueryCount(open_items),
                reviews_in_progress=SubqueryCount(direct_reviews),
            )
            .order_by("user__first_name", "user__last_name", "id")
        )

    @extend_schema(
        description=(
            "Set the reviewer's standing. A suspended or blocked reviewer is "
            "no longer selected for, invited to or offered new review work, "
            "and cannot accept pending invitations or assignments; work already "
            "accepted stays. Nothing is deleted. A reason is required unless "
            "the standing is active."
        ),
        request=serializers.ReviewerStandingSerializer,
        responses=serializers.ReviewerRegistrySerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="set-standing")
    def set_standing(self, request, uuid=None):
        profile = self.get_object()
        serializer = serializers.ReviewerStandingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reviewer_registry.set_standing(
            profile,
            serializer.validated_data["standing"],
            serializer.validated_data.get("reason", ""),
            request.user,
        )
        return response.Response(self.get_serializer(self.get_object()).data)

    @extend_schema(
        description="Approve the reviewer's pending request to publish their profile.",
        request=core_serializers.ReviewCommentSerializer,
        responses=serializers.ReviewerRegistrySerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="approve-publication")
    def approve_publication(self, request, uuid=None):
        return self._decide_publication(request, approve=True)

    @extend_schema(
        description=(
            "Reject the reviewer's pending request to publish their profile; "
            "the comment is shown to the reviewer."
        ),
        request=core_serializers.ReviewCommentSerializer,
        responses=serializers.ReviewerRegistrySerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="reject-publication")
    def reject_publication(self, request, uuid=None):
        return self._decide_publication(request, approve=False)

    def _decide_publication(self, request, approve):
        profile = self.get_object()
        serializer = core_serializers.ReviewCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reviewer_registry.decide_publication(
            profile,
            approve,
            request.user,
            serializer.validated_data.get("comment", ""),
        )
        return response.Response(self.get_serializer(self.get_object()).data)


# =============================================================================
# COI (Conflict of Interest) ViewSets
# =============================================================================


class ConflictOfInterestViewSet(ActionsViewSet):
    """ViewSet for managing conflicts of interest."""

    lookup_field = "uuid"
    queryset = models.ConflictOfInterest.objects.all().order_by("-detected_at")
    serializer_class = serializers.ConflictOfInterestSerializer
    filterset_class = filters.ConflictOfInterestFilter
    filter_backends = (DjangoFilterBackend,)
    disabled_actions = ["create", "destroy"]

    @access.shows(
        "Users with the staff or support flag, and holders of a site-wide "
        "CALL.LIST grant, see every conflict. Others see those about "
        "themselves as reviewer, and those of calls they hold the call manager "
        "role on, or any role on the call's managing organisation or on its "
        "customer, except on proposals they apply with."
    )
    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support or sees_every_call(user):
            return models.ConflictOfInterest.objects.all().order_by("-detected_at")
        # Call managers can see COIs for their calls -- not those about a
        # proposal they apply with: who is conflicted with it, and whether
        # that stands, is not the applicant's to read or decide.
        oversees = (
            Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ) & ~Q(proposal_id__in=proposal_roles.applicant_team_proposal_ids(user))
        return models.ConflictOfInterest.objects.filter(
            oversees | Q(reviewer__user=user)  # Reviewers can see their own COIs
        ).order_by("-detected_at")

    def _resolve(self, request, outcome):
        """Resolve a pending conflict as ``outcome``, under a lock.

        A conflict is resolved once: dismissed, waived or recused from
        pending, never moved from one outcome to another. Returns the
        locked, updated conflict and the validated request data.
        """
        coi = self.get_object()
        serializer = serializers.COIStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        coi = models.ConflictOfInterest.objects.select_for_update().get(pk=coi.pk)
        if coi.status != COIStatuses.PENDING:
            raise exceptions.ValidationError(
                {
                    "status": _(
                        "Only a pending conflict of interest can be resolved; "
                        "this one is already %(status)s."
                    )
                    % {"status": coi.status}
                }
            )
        if outcome == COIStatuses.WAIVED and coi_detection.requires_recusal(coi):
            raise exceptions.ValidationError(
                {
                    "status": _(
                        "The call requires recusal for conflicts of this type, "
                        "so it cannot be waived with a management plan."
                    )
                }
            )

        coi.status = outcome
        coi.reviewed_by = request.user
        coi.reviewed_at = timezone.now()
        coi.review_notes = serializer.validated_data.get("review_notes", "")
        return coi, serializer.validated_data

    @extend_schema(
        description="Dismiss a pending conflict of interest (not a real conflict).",
        request=serializers.COIStatusUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ConflictOfInterestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def dismiss(self, request, uuid=None):
        """Dismiss a detected conflict (mark as not a real conflict)."""
        coi, _data = self._resolve(request, COIStatuses.DISMISSED)
        coi.save()

        self._unblock_related_assignments(coi)

        return response.Response(
            self.get_serializer(coi).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Waive a pending conflict with a management plan. Refused "
        "for a type the call's COI settings require recusal for.",
        request=serializers.COIStatusUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ConflictOfInterestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def waive(self, request, uuid=None):
        """Waive a conflict with a management plan."""
        coi, data = self._resolve(request, COIStatuses.WAIVED)
        if not data.get("management_plan"):
            raise exceptions.ValidationError(
                {"management_plan": _("Management plan is required when waiving.")}
            )
        coi.management_plan = data["management_plan"]
        coi.save()

        self._unblock_related_assignments(coi)

        return response.Response(
            self.get_serializer(coi).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Recuse the reviewer from the proposal over a pending conflict.",
        request=serializers.COIStatusUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ConflictOfInterestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def recuse(self, request, uuid=None):
        """Recuse the reviewer from the proposal."""
        coi, _data = self._resolve(request, COIStatuses.RECUSED)
        coi.save()

        # Clean up any existing review assignments for this reviewer-proposal pair
        self._cleanup_reviewer_assignments(coi)

        return response.Response(
            self.get_serializer(coi).data,
            status=status.HTTP_200_OK,
        )

    def _cleanup_reviewer_assignments(self, coi: models.ConflictOfInterest):
        """
        Clean up any existing assignments and reviews when a reviewer is recused.

        This ensures:
        1. Any pending assignment items are marked as COI_BLOCKED
        2. Any active reviews are rejected
        3. The COI record is linked to the blocked assignments
        """
        reviewer = coi.reviewer
        proposal = coi.proposal

        if not reviewer or not proposal:
            return

        # Find assignment items for this reviewer-proposal pair
        assignment_items = models.AssignmentItem.objects.filter(
            proposal=proposal,
            batch__reviewer_pool_entry__reviewer=reviewer,
            status__in=[
                models.AssignmentItemStatuses.PENDING,
                models.AssignmentItemStatuses.ACCEPTED,
            ],
        )

        for item in assignment_items:
            # Mark the item as COI blocked
            item.status = models.AssignmentItemStatuses.COI_BLOCKED
            item.has_coi = True
            item.save(update_fields=["status", "has_coi"])
            item.coi_records.add(coi)

            # If the item had an associated review, reject it
            if item.review:
                item.review.state = models.Review.States.REJECTED
                item.review.save(update_fields=["state"])

        # Also reject any reviews directly (in case they exist without assignment items)
        models.Review.objects.filter(
            proposal=proposal,
            reviewer=reviewer.user,
            state__in=[models.Review.States.IN_REVIEW],
        ).update(state=models.Review.States.REJECTED)

        # An evaluator role the reviewer holds on the proposal goes too; the
        # standing conflict already stopped its access.
        for assignment in proposal_roles.revoke_on_recusal(
            reviewer.user, proposal, revoked_by=coi.reviewed_by
        ):
            activity.log_evaluator_revoked(assignment)
            assignment_uuid = assignment.uuid.hex
            transaction.on_commit(
                lambda uuid=assignment_uuid: tasks.notify_proposal_role_changed.delay(
                    uuid, False
                )
            )

    def _unblock_related_assignments(self, coi):
        """
        Unblock assignment items that were blocked by this COI.

        Only unblocks items if no other unresolved COIs remain for
        the same reviewer-proposal pair.
        """
        if not coi.proposal or not coi.reviewer:
            return

        blocked_items = models.AssignmentItem.objects.filter(
            proposal=coi.proposal,
            batch__reviewer_pool_entry__reviewer=coi.reviewer,
            status=models.AssignmentItemStatuses.COI_BLOCKED,
        )

        for item in blocked_items:
            # Check if there are other unresolved COIs (pending or recused)
            # excluding the one being dismissed/waived
            other_blocking_cois = item.coi_records.filter(
                status__in=[COIStatuses.PENDING, COIStatuses.RECUSED],
            ).exclude(pk=coi.pk)

            if not other_blocking_cois.exists():
                item.status = models.AssignmentItemStatuses.PENDING
                item.has_coi = False
                item.save(update_fields=["status", "has_coi"])
                item.coi_records.remove(coi)

    # The update route is included: it was ungated, so the reviewer a conflict
    # is about could PATCH their own record.
    dismiss_permissions = waive_permissions = recuse_permissions = (
        update_permissions
    ) = partial_update_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]
    dismiss_serializer_class = waive_serializer_class = recuse_serializer_class = (
        serializers.COIStatusUpdateSerializer
    )


class COIDisclosureViewSet(ActionsViewSet):
    """ViewSet for COI disclosure forms."""

    lookup_field = "uuid"
    queryset = models.COIDisclosureForm.objects.all().order_by("-created")
    serializer_class = serializers.COIDisclosureFormSerializer
    filterset_class = filters.COIDisclosureFormFilter
    filter_backends = (DjangoFilterBackend,)
    disabled_actions = ["update", "partial_update", "destroy"]
    access_declarations = {
        "create": [
            access.owned("A disclosure filed under the caller's own reviewer profile.")
        ],
    }

    @access.shows(
        "Users with the staff flag see every disclosure. Others see their own, "
        "and those of calls they hold the call manager role on, or any role on "
        "the call's managing organisation or on its customer."
    )
    def get_queryset(self):
        user = self.request.user
        # Staff only. Support and a call desk stay on the membership filter.
        if user.is_staff:
            return models.COIDisclosureForm.objects.all().order_by("-created")
        # Users can see their own disclosures, call managers can see all for their calls
        return models.COIDisclosureForm.objects.filter(
            Q(reviewer__user=user)
            | Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-created")

    def perform_create(self, serializer):
        # Get or create reviewer profile
        try:
            reviewer_profile = models.ReviewerProfile.objects.get(
                user=self.request.user
            )
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must create a reviewer profile before submitting a disclosure.")
            )
        serializer.save(
            reviewer=reviewer_profile,
            certified=True,
            certification_date=timezone.now(),
        )


# =============================================================================
# Invitation Acceptance Mixin (shared logic for pool invitation handling)
# =============================================================================


def expired_response_error(kind: str, expires_at) -> str:
    """Why a reviewer can no longer answer an assignment or invitation."""
    if expires_at is None:
        return _(
            "This {kind} has expired. Ask the call manager to send it again."
        ).format(kind=kind)
    return _(
        "This {kind} expired on {date}. Ask the call manager to send it again."
    ).format(
        kind=kind, date=timezone.localtime(expires_at).strftime("%Y-%m-%d %H:%M %Z")
    )


class InvitationAcceptanceMixin:
    """
    Mixin providing common logic for accepting/declining reviewer pool invitations.

    Used by both CallReviewerPoolViewSet and PublicReviewerInvitationViewSet
    to avoid code duplication.
    """

    def _validate_invitation_status(self, invitation: models.CallReviewerPool):
        """Validate that the invitation is still pending."""
        if invitation.invitation_status != ReviewerPoolInvitationStatuses.PENDING:
            raise exceptions.ValidationError(
                _("This invitation has already been responded to.")
            )

    def _validate_invitation_not_expired(self, invitation: models.CallReviewerPool):
        """Validate that the invitation has not expired."""
        if invitation.is_invitation_expired:
            raise exceptions.ValidationError(
                expired_response_error(
                    _("invitation"), invitation.invitation_expires_at
                )
            )

    def _validate_invitation_answerable(self, invitation: models.CallReviewerPool):
        """An invitation can be accepted or declined only while pending and in date."""
        self._validate_invitation_not_expired(invitation)
        self._validate_invitation_status(invitation)

    def _ensure_published_profile(
        self, request, invitation: models.CallReviewerPool
    ) -> tuple[models.ReviewerProfile | None, dict | None]:
        """
        Ensure the user has a published reviewer profile for email-based invitations.

        Returns:
            tuple: (profile, error_response) - profile if found, or error_response dict if not
        """
        if invitation.reviewer:
            return invitation.reviewer, None

        # Check if user is authenticated
        if not request.user.is_authenticated:
            return None, {
                "error": _("Please log in to accept this invitation."),
                "status": status.HTTP_401_UNAUTHORIZED,
            }

        # Check if user has a published profile
        try:
            profile = models.ReviewerProfile.objects.get(
                user=request.user,
                is_published=True,
            )
            return profile, None
        except models.ReviewerProfile.DoesNotExist:
            has_unpublished = models.ReviewerProfile.objects.filter(
                user=request.user, is_published=False
            ).exists()

            awaiting_approval = models.ReviewerPublicationRequest.objects.filter(
                profile__user=request.user, state=CoreReviewStates.PENDING
            ).exists()
            if awaiting_approval:
                return None, {
                    "error": "profile_awaiting_approval",
                    "message": _(
                        "Your reviewer profile is awaiting the operator's "
                        "approval before it can be published. You can accept "
                        "once it is approved."
                    ),
                    "profile_url": "/api/reviewer-profiles/me/",
                    "status": status.HTTP_400_BAD_REQUEST,
                }
            if has_unpublished:
                return None, {
                    "error": "profile_not_published",
                    "message": _(
                        "Please publish your reviewer profile before accepting."
                    ),
                    "profile_url": "/api/reviewer-profiles/me/",
                    "status": status.HTTP_400_BAD_REQUEST,
                }
            else:
                return None, {
                    "error": "profile_required",
                    "message": _(
                        "Please create and publish your reviewer profile first."
                    ),
                    "profile_url": "/api/reviewer-profiles/me/",
                    "status": status.HTTP_400_BAD_REQUEST,
                }

    def _process_self_declared_conflicts(
        self,
        declared_conflicts: list,
        invitation: models.CallReviewerPool,
    ) -> list[str]:
        """
        Process self-declared conflicts from invitation acceptance.

        Args:
            declared_conflicts: List of conflict data from request
            invitation: The invitation being accepted

        Returns:
            List of created conflict UUIDs
        """
        if not declared_conflicts:
            return []

        conflict_serializer = serializers.SelfDeclaredConflictSerializer(
            data=declared_conflicts,
            many=True,
            context={"call": invitation.call, "reviewer": invitation.reviewer},
        )
        conflict_serializer.is_valid(raise_exception=True)

        created_conflicts = []
        for conflict_data in conflict_serializer.validated_data:
            coi = models.ConflictOfInterest.objects.create(
                reviewer=invitation.reviewer,
                call=invitation.call,
                proposal=conflict_data["proposal_uuid"],
                coi_type=conflict_data["coi_type"],
                # The reviewer's own reading, else how the call handles the
                # type, else the type's default.
                severity=conflict_data.get("severity")
                or coi_detection.default_severity(
                    conflict_data["coi_type"],
                    models.CallCOIConfiguration.objects.filter(
                        call=invitation.call
                    ).first(),
                ),
                detection_method=COIDetectionMethods.SELF_DISCLOSED,
                evidence_description=conflict_data.get("description", ""),
                status=COIStatuses.PENDING,
            )
            created_conflicts.append(str(coi.uuid))

        return created_conflicts

    def _link_profile(self, invitation: models.CallReviewerPool, profile, user) -> None:
        """Bind an email invitation to the reviewer profile accepting it."""
        if invitation.reviewer:
            return
        # The invitee's to accept, whoever else holds the link
        if not invitation.is_addressed_to(user):
            raise exceptions.PermissionDenied(
                _("This invitation was sent to another reviewer.")
            )
        # The same person may also have been invited by profile; binding this
        # row too would violate unique_call_reviewer
        if (
            models.CallReviewerPool.objects.filter(
                call=invitation.call, reviewer=profile
            )
            .exclude(pk=invitation.pk)
            .exists()
        ):
            raise exceptions.ValidationError(
                _("You are already in this call's reviewer pool.")
            )
        invitation.reviewer = profile
        # The account that accepted, which may be another account of the
        # invited address than the one picked at invitation time
        invitation.invited_user = user

    def _accept_invitation(
        self, invitation: models.CallReviewerPool
    ) -> models.CallReviewerPool:
        """Mark the invitation as accepted."""
        invitation.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
        invitation.response_date = timezone.now()
        invitation.save()
        return invitation

    def _decline_invitation(
        self, invitation: models.CallReviewerPool, reason: str = ""
    ) -> models.CallReviewerPool:
        """Mark the invitation as declined."""
        invitation.invitation_status = ReviewerPoolInvitationStatuses.DECLINED
        invitation.response_date = timezone.now()
        invitation.decline_reason = reason
        invitation.save()
        return invitation


class CallReviewerPoolViewSet(InvitationAcceptanceMixin, ActionsViewSet):
    """ViewSet for call reviewer pool management."""

    lookup_field = "uuid"
    queryset = models.CallReviewerPool.objects.all().order_by("call", "reviewer")
    serializer_class = serializers.CallReviewerPoolSerializer
    filterset_class = filters.CallReviewerPoolFilter
    filter_backends = (DjangoFilterBackend,)

    # Only allow partial_update (PATCH), disable create/update/destroy
    disabled_actions = ["create", "update", "destroy"]

    partial_update_serializer_class = serializers.CallReviewerPoolUpdateSerializer
    partial_update_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]
    # Checked in the action, after the entry is looked up as retrieve does.
    _ADDRESSEE = [
        access.owned(
            "The invitation's addressee: the reviewer whose profile it names, "
            "or, before a profile is linked, the invited user or the holder of "
            "the invited email."
        )
    ]
    access_declarations = {"accept": _ADDRESSEE, "decline": _ADDRESSEE}

    @access.shows(
        "Users with the staff or support flag, and holders of a site-wide "
        "CALL.LIST grant, see every entry. Others see the entries addressed to "
        "them, and those of calls they hold the call manager role on, or any "
        "role on the call's managing organisation or on its customer."
    )
    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support or sees_every_call(user):
            qs = models.CallReviewerPool.objects.all()
        else:
            qs = models.CallReviewerPool.objects.filter(
                Q(call__in=get_connected_calls(user, CallRole.MANAGER))
                | Q(call__manager__customer__in=get_connected_customers(user))
                | Q(
                    call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                        user
                    )
                )
                | models.CallReviewerPool.addressed_to(user)
            )
        # Add select_related to avoid N+1 on related fields
        return (
            qs.with_open_assignments()
            .select_related(
                "call",
                "reviewer",
                "reviewer__user",
                "invited_by",
                "invited_user",
            )
            .order_by("call", "reviewer")
        )

    def get_serializer_context(self):
        """Add prefetched COI and review counts to context to avoid N+1 queries."""
        context = super().get_serializer_context()

        # Only prefetch for list actions
        if self.action not in ["list", "retrieve"]:
            return context

        # Get the queryset that will be serialized
        queryset = self.filter_queryset(self.get_queryset())
        pool_members = list(queryset)

        if not pool_members:
            return context

        # Collect all call_ids and reviewer_ids
        call_ids = {pm.call_id for pm in pool_members}
        reviewer_ids = {pm.reviewer_id for pm in pool_members if pm.reviewer_id}
        user_ids = set()
        for pm in pool_members:
            if pm.reviewer and pm.reviewer.user_id:
                user_ids.add(pm.reviewer.user_id)
            elif pm.invited_user_id:
                user_ids.add(pm.invited_user_id)

        # Prefetch COI counts: (reviewer_id, call_id) -> count
        coi_counts = {}
        coi_by_severity = {}
        if reviewer_ids and call_ids:
            coi_data = (
                models.ConflictOfInterest.objects.filter(
                    reviewer_id__in=reviewer_ids,
                    call_id__in=call_ids,
                )
                .values("reviewer_id", "call_id", "severity")
                .annotate(count=Count("id"))
            )
            for item in coi_data:
                key = (item["reviewer_id"], item["call_id"])
                coi_counts[key] = coi_counts.get(key, 0) + item["count"]
                if key not in coi_by_severity:
                    coi_by_severity[key] = {}
                coi_by_severity[key][item["severity"]] = item["count"]

        # Prefetch review counts: (user_id, call_id) -> {state: count}
        review_counts = {}
        if user_ids and call_ids:
            review_data = (
                models.Review.objects.filter(
                    reviewer_id__in=user_ids,
                    proposal__round__call_id__in=call_ids,
                )
                .values("reviewer_id", "proposal__round__call_id", "state")
                .annotate(count=Count("id"))
            )
            for item in review_data:
                key = (item["reviewer_id"], item["proposal__round__call_id"])
                if key not in review_counts:
                    review_counts[key] = {}
                review_counts[key][item["state"]] = item["count"]

        context["coi_counts"] = coi_counts
        context["coi_by_severity"] = coi_by_severity
        context["review_counts"] = review_counts

        return context

    def _verify_invitation_ownership(self, invitation):
        """Verify the current user owns this invitation."""
        return invitation.is_addressed_to(self.request.user)

    @extend_schema(
        description="Accept a pool invitation (authenticated users only).",
        request=serializers.SelfDeclaredConflictSerializer(many=True),
        responses={
            200: serializers.InvitationAcceptResponseSerializer,
            400: serializers.InvitationAcceptErrorSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        """Accept a pool invitation."""
        invitation = self.get_object()

        # Verify ownership
        if not self._verify_invitation_ownership(invitation):
            raise exceptions.PermissionDenied(
                _("You do not have permission to accept this invitation.")
            )

        self._validate_invitation_answerable(invitation)

        # Profile-gating: user must have a published reviewer profile
        profile, error = self._ensure_published_profile(request, invitation)
        if error:
            error_status = error.pop("status", status.HTTP_400_BAD_REQUEST)
            return response.Response(error, status=error_status)
        # Accepting takes on review work: refused to a reviewer the operator
        # has suspended or blocked, or outside the call's reviewer eligibility.
        reviewer_standing.ensure_reviewer_eligible(profile.user, invitation.call)

        self._link_profile(invitation, profile, request.user)

        # Process optional self-declared conflicts
        # Body is the array of conflicts directly (not wrapped in a dict)
        declared_conflicts = request.data if isinstance(request.data, list) else []
        created_conflicts = self._process_self_declared_conflicts(
            declared_conflicts, invitation
        )

        self._accept_invitation(invitation)

        result = {"detail": _("Invitation accepted successfully.")}
        if created_conflicts:
            result["declared_conflicts"] = created_conflicts
        return response.Response(result)

    @extend_schema(
        description="Decline a pool invitation (authenticated users only).",
        request=serializers.InvitationDeclineSerializer,
        responses={200: serializers.InvitationDeclineResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        """Decline a pool invitation."""
        invitation = self.get_object()

        # Verify ownership
        if not self._verify_invitation_ownership(invitation):
            raise exceptions.PermissionDenied(
                _("You do not have permission to decline this invitation.")
            )

        self._validate_invitation_answerable(invitation)

        serializer = serializers.InvitationDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        self._decline_invitation(
            invitation, serializer.validated_data.get("reason", "")
        )

        return response.Response({"detail": _("Invitation declined.")})

    @extend_schema(
        description="Force-accept a pool invitation (manager override).",
        request=serializers.ForceAcceptPoolSerializer,
        responses={200: serializers.CallReviewerPoolSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="force-accept")
    def force_accept(self, request, uuid=None):
        """Force-accept a pool invitation with a reason."""
        invitation = self.get_object()

        if invitation.invitation_status == ReviewerPoolInvitationStatuses.ACCEPTED:
            return response.Response(
                {"error": _("This invitation is already accepted.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if invitation.invitation_status not in [
            ReviewerPoolInvitationStatuses.PENDING,
            ReviewerPoolInvitationStatuses.DECLINED,
            ReviewerPoolInvitationStatuses.EXPIRED,
        ]:
            return response.Response(
                {"error": _("This invitation cannot be force-accepted.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not invitation.reviewer:
            return response.Response(
                {
                    "error": _(
                        "Cannot force-accept an email-only invitation without a reviewer profile."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ForceAcceptPoolSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # The call's managers override the reviewer's answer, not the
        # operator's standing or the call's reviewer eligibility.
        reviewer_standing.ensure_reviewer_eligible(
            invitation.reviewer.user, invitation.call
        )

        invitation.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
        invitation.response_date = timezone.now()
        invitation.override_reason = serializer.validated_data["override_reason"]
        invitation.overridden_by = request.user
        invitation.overridden_at = timezone.now()
        invitation.save()
        event_logger.emit(
            "%(user)s accepted the reviewer pool invitation of %(reviewer)s on "
            "call %(call)s on their behalf: %(reason)s"
            % {
                "user": request.user.full_name or request.user.username,
                "reviewer": invitation.reviewer.user.full_name,
                "call": invitation.call.name,
                "reason": invitation.override_reason,
            },
            event_type=EventType.REVIEWER_INVITATION_FORCE_ACCEPTED,
            event_context={
                "call": invitation.call,
                "reviewer_uuid": invitation.reviewer.uuid.hex,
                "reason": invitation.override_reason,
            },
            scopes=[_get_customer(invitation.call)],
        )

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                invitation, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    force_accept_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]

    @extend_schema(
        description=(
            "Send a pending or expired pool invitation again. The invitation "
            "returns to pending with a new expiry date from the call's "
            "assignment configuration, and the invitee is emailed the link again."
        ),
        request=None,
        responses={200: serializers.CallReviewerPoolSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="resend-invitation")
    def resend_invitation(self, request, uuid=None):
        """Send a pending or expired pool invitation again."""
        invitation = reviewer_pool.resend_invitation(self.get_object())

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                invitation, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    resend_invitation_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]


class COIDetectionJobViewSet(ReadOnlyActionsViewSet):
    """ViewSet for viewing COI detection job status."""

    lookup_field = "uuid"
    queryset = models.COIDetectionJob.objects.all().order_by("-created")
    serializer_class = serializers.COIDetectionJobSerializer
    filterset_class = filters.COIDetectionJobFilter
    filter_backends = (DjangoFilterBackend,)

    @access.shows(
        "Users with the staff flag see every job. Others see the jobs of calls "
        "they hold the call manager role on, or any role on the call's "
        "managing organisation or on its customer."
    )
    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.COIDetectionJob.objects.all().order_by("-created")
        return models.COIDetectionJob.objects.filter(
            Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-created")


# =============================================================================
# Reviewer Suggestion ViewSet
# =============================================================================


class ReviewerSuggestionViewSet(ReadOnlyActionsViewSet):
    """ViewSet for managing algorithm-generated reviewer suggestions."""

    lookup_field = "uuid"
    queryset = models.ReviewerSuggestion.objects.all().order_by("-affinity_score")
    serializer_class = serializers.ReviewerSuggestionSerializer
    filterset_class = filters.ReviewerSuggestionFilter
    filter_backends = (DjangoFilterBackend,)
    # Allow destroy action (override ReadOnlyActionsViewSet defaults)
    disabled_actions = ["create", "update", "partial_update"]

    # Checked in the action body, on a suggestion get_object() found.
    access_declarations = {
        action: [
            access.custom(
                "A request acting as staff, the call's managers, and holders of "
                "any role on the organization that runs the call's managing "
                "organisation."
            )
        ]
        for action in ("confirm", "reject")
    }

    @access.shows(
        "Suggestions for calls the caller manages, calls run by an organization "
        "they hold any role on, and calls of a call-managing organisation they "
        "hold a role on; users with the staff or support flag and holders of a "
        "site-wide CALL.LIST grant see all."
    )
    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support or sees_every_call(user):
            return models.ReviewerSuggestion.objects.all().order_by("-affinity_score")
        return models.ReviewerSuggestion.objects.filter(
            Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-affinity_score")

    @extend_schema(
        description="Delete a reviewer suggestion.",
        responses={204: None},
    )
    def destroy(self, request, uuid=None):
        """Delete a reviewer suggestion."""
        suggestion = self.get_object()

        # Check if user has permission to manage this call
        user = request.user
        call = suggestion.call
        if not has_staff_access(request) and not (
            call.id in get_connected_calls(user, CallRole.MANAGER)
            or call.manager.customer_id in get_connected_customers(user)
        ):
            raise exceptions.PermissionDenied(
                _("You do not have permission to delete suggestions for this call.")
            )

        suggestion.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]

    @extend_schema(
        description="Confirm a reviewer suggestion. The reviewer will be invited to the call.",
        responses={200: serializers.ReviewerSuggestionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def confirm(self, request, uuid=None):
        """Manager confirms a reviewer suggestion."""
        suggestion = self.get_object()

        # Check if user has permission to manage this call
        user = request.user
        call = suggestion.call
        if not has_staff_access(request) and not (
            call.id in get_connected_calls(user, CallRole.MANAGER)
            or call.manager.customer_id in get_connected_customers(user)
        ):
            raise exceptions.PermissionDenied(
                _("You do not have permission to confirm suggestions for this call.")
            )

        if suggestion.status != ReviewerSuggestionStatuses.PENDING:
            raise exceptions.ValidationError(
                _("Only pending suggestions can be confirmed.")
            )

        suggestion.status = ReviewerSuggestionStatuses.CONFIRMED
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.save()

        return response.Response(
            serializers.ReviewerSuggestionSerializer(
                suggestion, context={"request": request}
            ).data
        )

    @extend_schema(
        description="Reject a reviewer suggestion.",
        request=serializers.SuggestionRejectSerializer,
        responses={200: serializers.ReviewerSuggestionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        """Manager rejects a reviewer suggestion."""
        suggestion = self.get_object()

        # Check if user has permission to manage this call
        user = request.user
        call = suggestion.call
        if not has_staff_access(request) and not (
            call.id in get_connected_calls(user, CallRole.MANAGER)
            or call.manager.customer_id in get_connected_customers(user)
        ):
            raise exceptions.PermissionDenied(
                _("You do not have permission to reject suggestions for this call.")
            )

        if suggestion.status != ReviewerSuggestionStatuses.PENDING:
            raise exceptions.ValidationError(
                _("Only pending suggestions can be rejected.")
            )

        reason = request.data.get("reason", "")
        suggestion.status = ReviewerSuggestionStatuses.REJECTED
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.rejection_reason = reason
        suggestion.save()

        return response.Response(
            serializers.ReviewerSuggestionSerializer(
                suggestion, context={"request": request}
            ).data
        )


# =============================================================================
# Public Reviewer Invitation ViewSet
# =============================================================================


class PublicReviewerInvitationViewSet(InvitationAcceptanceMixin, viewsets.ViewSet):
    """
    Public endpoints for handling reviewer invitations via token.

    These endpoints do not require authentication - the invitation token
    serves as authorization.
    """

    permission_classes = []  # Public - no auth required

    # Answering takes signing in as the invitation's addressee
    # (CallReviewerPool.is_addressed_to); the token only finds it.
    _INVITEE = [
        access.owned(
            "A signed-in addressee with the invitation token: the reviewer whose "
            "profile the invitation names or, before a profile is linked, the "
            "invited user or any user with the invited e-mail address."
        )
    ]
    access_declarations = {
        # Anyone holding the invitation token from the e-mail reads the call's
        # name, the invitation's status and deadline and who sent it -- no
        # proposals -- and, when signed in, whether they have a reviewer profile.
        "retrieve": [access.public()],
        "accept": _INVITEE,
        "decline": _INVITEE,
    }

    def _get_invitation(self, token: str) -> models.CallReviewerPool:
        """Get and validate invitation by token."""
        try:
            invitation = models.CallReviewerPool.objects.select_related(
                "call", "reviewer__user"
            ).get(invitation_token=token)
        except models.CallReviewerPool.DoesNotExist:
            raise exceptions.NotFound(_("Invalid invitation token."))

        return invitation

    @extend_schema(
        description="Get invitation details by token.",
        responses=serializers.PublicInvitationSerializer,
    )
    def retrieve(self, request, token=None):
        """Get invitation details by token including COI configuration."""
        invitation = self._get_invitation(token)
        call = invitation.call

        is_expired = invitation.is_invitation_expired

        # Check user's profile status if authenticated
        profile_status = None
        if request.user.is_authenticated:
            profile = models.ReviewerProfile.objects.filter(user=request.user).first()
            if profile:
                profile_status = "published" if profile.is_published else "unpublished"
            else:
                profile_status = "missing"

        # Get COI configuration if available (informational only at invitation stage)
        coi_config = None
        if hasattr(call, "coi_configuration") and call.coi_configuration:
            config = call.coi_configuration
            coi_config = {
                "recusal_required_types": config.recusal_required_types,
                "management_allowed_types": config.management_allowed_types,
                "disclosure_only_types": config.disclosure_only_types,
            }

        # Note: Proposals are NOT included at invitation stage.
        # They are only disclosed during the assignment stage (two-step workflow).
        return response.Response(
            {
                "call_name": call.name,
                "call_uuid": str(call.uuid),
                "invitation_status": invitation.invitation_status,
                "invited_at": invitation.invited_at,
                "expires_at": invitation.invitation_expires_at,
                "is_expired": is_expired,
                "max_assignments": invitation.max_assignments,
                "invited_by_name": (
                    invitation.invited_by.full_name if invitation.invited_by else None
                ),
                "profile_status": profile_status,
                "requires_profile": invitation.reviewer is None,
                "coi_configuration": coi_config,
                "coi_types": COITypes.CHOICES,
            }
        )

    @extend_schema(
        description="Accept a reviewer invitation.",
        request=serializers.InvitationAcceptSerializer,
        responses={
            200: serializers.InvitationAcceptResponseSerializer,
            400: serializers.InvitationAcceptErrorSerializer,
            401: serializers.InvitationAuthErrorSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, token=None):
        """Accept a reviewer invitation."""
        invitation = self._get_invitation(token)

        # Only the invitee accepts: the token by itself is not enough, as the
        # link may have been forwarded. For an address that had no account at
        # invitation time, that is a signed-in user with that address -- the
        # one an invitee registering from the e-mail signs up with.
        if not request.user.is_authenticated:
            return response.Response(
                {"error": _("Please log in to accept this invitation.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not invitation.is_addressed_to(request.user):
            raise exceptions.PermissionDenied(
                _("This invitation was sent to another reviewer.")
            )

        self._validate_invitation_answerable(invitation)

        # Profile-gating for email invitations
        profile, error = self._ensure_published_profile(request, invitation)
        if error:
            error_status = error.pop("status", status.HTTP_400_BAD_REQUEST)
            return response.Response(error, status=error_status)
        # Accepting takes on review work: refused to a reviewer the operator
        # has suspended or blocked, or outside the call's reviewer eligibility.
        reviewer_standing.ensure_reviewer_eligible(profile.user, invitation.call)

        self._link_profile(invitation, profile, request.user)

        # Process optional self-declared conflicts
        declared_conflicts = request.data.get("declared_conflicts", [])
        created_conflicts = self._process_self_declared_conflicts(
            declared_conflicts, invitation
        )

        self._accept_invitation(invitation)

        result = {"detail": _("Invitation accepted successfully.")}
        if created_conflicts:
            result["declared_conflicts"] = created_conflicts
        return response.Response(result)

    @extend_schema(
        description="Decline a reviewer invitation. Requires signing in as the "
        "invitee; for an invitation sent to an address with no account, the "
        "signed-in user's e-mail must match it.",
        request=serializers.InvitationDeclineSerializer,
        responses={
            200: serializers.InvitationDeclineResponseSerializer,
            401: serializers.InvitationAuthErrorSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def decline(self, request, token=None):
        """Decline a reviewer invitation."""
        invitation = self._get_invitation(token)

        # A declined invitation cannot be sent again, so the link alone, which
        # may have been forwarded, is not enough to decline it.
        if not request.user.is_authenticated:
            return response.Response(
                {"error": _("Please log in to decline this invitation.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not invitation.is_addressed_to(request.user):
            raise exceptions.PermissionDenied(
                _("This invitation was sent to another reviewer.")
            )

        self._validate_invitation_answerable(invitation)

        serializer = serializers.InvitationDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        self._decline_invitation(
            invitation, serializer.validated_data.get("reason", "")
        )

        return response.Response({"detail": _("Invitation declined.")})


# =============================================================================
# Reviewer Bids ViewSet
# =============================================================================


@access.describes(
    access.owned(
        "The bid's own reviewer, while they are an accepted member of the "
        "reviewer pool of the bid's call, in good standing."
    ),
    needs_object=True,
)
def is_eligible_bid_reviewer(request, view, obj: models.ReviewerBid | None = None):
    # Whoever may place the bid through submit may change or withdraw it.
    # Nobody else, staff included, may speak for the reviewer.
    if obj is None:
        return
    if obj.reviewer.user_id != request.user.pk:
        raise exceptions.PermissionDenied(
            _("Only the reviewer who placed a bid may change or withdraw it.")
        )
    if (
        not models.CallReviewerPool.objects.eligible()
        .filter(call=obj.call, reviewer=obj.reviewer)
        .exists()
    ):
        raise exceptions.PermissionDenied(
            _(
                "Only an accepted member of the call's reviewer pool, in good "
                "standing, may change or withdraw a bid."
            )
        )


class ReviewerBidViewSet(ActionsViewSet):
    """
    ViewSet for managing reviewer bids on proposals.

    Reviewers can indicate their preference/availability for reviewing proposals.
    """

    lookup_field = "uuid"
    queryset = models.ReviewerBid.objects.all().order_by("-submitted_at")
    serializer_class = serializers.ReviewerBidSerializer
    filterset_class = filters.ReviewerBidFilter
    filter_backends = (DjangoFilterBackend,)
    # Bids are placed through submit and bulk_submit, which take the reviewer
    # and the call from the caller and the proposal.
    disabled_actions = ["create"]
    update_permissions = partial_update_permissions = destroy_permissions = [
        is_eligible_bid_reviewer
    ]

    access_declarations = {
        "my_bids": [access.owned("The bids of the caller's own reviewer profile.")],
        "submit_bid": [
            access.owned(
                "The caller bids from their own reviewer profile, on proposals "
                "of calls whose reviewer pool they are an accepted member of, "
                "in good standing."
            )
        ],
        "bulk_submit": [
            access.owned(
                "The caller bids from their own reviewer profile, on proposals "
                "of calls whose reviewer pool they are an accepted member of, "
                "in good standing; other bids are reported as errors."
            )
        ],
    }

    @access.shows(
        "The caller's own bids, and bids on calls they manage, calls run by an "
        "organization they hold any role on, and calls of a call-managing "
        "organisation they hold a role on; users with the staff flag see all."
    )
    def get_queryset(self):
        user = self.request.user
        # Staff only. Support and a call desk stay on the membership filter.
        if user.is_staff:
            return models.ReviewerBid.objects.all().order_by("-submitted_at")
        # Reviewers can see their own bids, managers can see all for their calls
        return models.ReviewerBid.objects.filter(
            Q(reviewer__user=user)
            | Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-submitted_at")

    @extend_schema(
        description="Get my bids for a specific call.",
        responses=serializers.ReviewerBidSerializer(many=True),
    )
    @decorators.action(detail=False, methods=["get"], url_path="my-bids")
    def my_bids(self, request):
        """Get current user's bids, optionally filtered by call."""
        call_uuid = request.query_params.get("call_uuid")

        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            return response.Response([])

        bids = models.ReviewerBid.objects.filter(reviewer=profile)
        if call_uuid:
            bids = bids.filter(call__uuid=call_uuid)

        serializer = self.get_serializer(bids, many=True)
        return response.Response(serializer.data)

    @extend_schema(
        description="Submit a bid on a proposal.",
        request=serializers.ReviewerBidSubmitSerializer,
        responses=serializers.ReviewerBidSerializer,
    )
    @decorators.action(detail=False, methods=["post"], url_path="submit")
    def submit_bid(self, request):
        """Submit a bid on a proposal."""
        serializer = serializers.ReviewerBidSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must have a reviewer profile to submit bids.")
            )

        try:
            proposal = models.Proposal.objects.get(
                uuid=serializer.validated_data["proposal_uuid"]
            )
        except models.Proposal.DoesNotExist:
            raise exceptions.ValidationError(
                {"proposal_uuid": _("Proposal not found.")}
            )

        # Get the call from the proposal
        call = proposal.round.call if hasattr(proposal, "round") else None
        if not call:
            raise exceptions.ValidationError(
                _("Could not determine the call for this proposal.")
            )

        # Check if reviewer is in the pool for this call
        if (
            not models.CallReviewerPool.objects.eligible()
            .filter(
                call=call,
                reviewer=profile,
            )
            .exists()
        ):
            raise exceptions.ValidationError(
                _("You are not in the reviewer pool for this call.")
            )

        # Create or update bid
        bid, created = models.ReviewerBid.objects.update_or_create(
            call=call,
            reviewer=profile,
            proposal=proposal,
            defaults={
                "bid": serializer.validated_data["bid"],
                "comment": serializer.validated_data.get("comment", ""),
            },
        )

        return response.Response(
            self.get_serializer(bid).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        description="Submit multiple bids at once.",
        request=serializers.ReviewerBulkBidSerializer,
        responses={
            200: {"type": "object", "properties": {"submitted": {"type": "integer"}}}
        },
    )
    @decorators.action(detail=False, methods=["post"], url_path="bulk-submit")
    def bulk_submit(self, request):
        """Submit multiple bids at once."""
        serializer = serializers.ReviewerBulkBidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must have a reviewer profile to submit bids.")
            )

        submitted = 0
        errors = []

        for bid_data in serializer.validated_data["bids"]:
            try:
                proposal = models.Proposal.objects.get(uuid=bid_data["proposal_uuid"])
                call = proposal.round.call if hasattr(proposal, "round") else None

                if not call:
                    errors.append(f"No call for proposal {bid_data['proposal_uuid']}")
                    continue
                # As for a single bid: only an accepted pool member of the call,
                # in good standing, bids on its proposals.
                if (
                    not models.CallReviewerPool.objects.eligible()
                    .filter(call=call, reviewer=profile)
                    .exists()
                ):
                    errors.append(
                        f"Not in the reviewer pool for proposal "
                        f"{bid_data['proposal_uuid']}"
                    )
                    continue

                models.ReviewerBid.objects.update_or_create(
                    call=call,
                    reviewer=profile,
                    proposal=proposal,
                    defaults={
                        "bid": bid_data["bid"],
                        "comment": bid_data.get("comment", ""),
                    },
                )
                submitted += 1
            except models.Proposal.DoesNotExist:
                errors.append(f"Proposal {bid_data['proposal_uuid']} not found")

        return response.Response(
            {
                "submitted": submitted,
                "errors": errors if errors else None,
            }
        )


# Nested ViewSets for ReviewerProfile sub-resources

# Their check_permission, run by get_queryset and perform_create, refuses
# everyone but the profile's own user and a request acting as staff.
REVIEWER_PROFILE_PART_ACCESS = {
    action: [
        access.any_of(
            access.owned("A part of the caller's own reviewer profile."),
            access.staff(),
        )
    ]
    for action in (
        "list",
        "retrieve",
        "create",
        "update",
        "partial_update",
        "destroy",
    )
}


@extend_schema_view(
    list=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_list",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    create=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_create",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    retrieve=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_retrieve",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    update=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    partial_update=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_partial_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    destroy=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_destroy",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
)
class ReviewerProfileAffiliationViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for managing reviewer profile affiliations."""

    lookup_field = "uuid"
    queryset = models.ReviewerAffiliation.objects.all()
    serializer_class = serializers.ReviewerAffiliationSerializer
    filter_backends = (DjangoFilterBackend,)

    access_declarations = REVIEWER_PROFILE_PART_ACCESS

    def get_reviewer_profile(self):
        profile_uuid = self.kwargs.get("reviewer_profile_uuid")
        return get_object_or_404(models.ReviewerProfile, uuid=profile_uuid)

    def check_permission(self):
        profile = self.get_reviewer_profile()
        user = self.request.user
        # Only profile owner or staff can manage affiliations
        if not (has_staff_access(self.request) or profile.user == user):
            raise exceptions.PermissionDenied(
                _("You do not have permission to manage this reviewer profile.")
            )

    def get_queryset(self):
        self.check_permission()
        profile = self.get_reviewer_profile()
        return profile.affiliations.all()

    def perform_create(self, serializer):
        self.check_permission()
        profile = self.get_reviewer_profile()
        serializer.save(reviewer_profile=profile)


@extend_schema_view(
    list=extend_schema(
        operation_id="nested_reviewer_profile_expertise_list",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    create=extend_schema(
        operation_id="nested_reviewer_profile_expertise_create",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    retrieve=extend_schema(
        operation_id="nested_reviewer_profile_expertise_retrieve",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    update=extend_schema(
        operation_id="nested_reviewer_profile_expertise_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    partial_update=extend_schema(
        operation_id="nested_reviewer_profile_expertise_partial_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    destroy=extend_schema(
        operation_id="nested_reviewer_profile_expertise_destroy",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
)
class ReviewerProfileExpertiseViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for managing reviewer profile expertise."""

    lookup_field = "uuid"
    queryset = models.ReviewerExpertise.objects.all()
    serializer_class = serializers.ReviewerExpertiseSerializer
    filter_backends = (DjangoFilterBackend,)

    access_declarations = REVIEWER_PROFILE_PART_ACCESS

    def get_reviewer_profile(self):
        profile_uuid = self.kwargs.get("reviewer_profile_uuid")
        return get_object_or_404(models.ReviewerProfile, uuid=profile_uuid)

    def check_permission(self):
        profile = self.get_reviewer_profile()
        user = self.request.user
        # Only profile owner or staff can manage expertise
        if not (has_staff_access(self.request) or profile.user == user):
            raise exceptions.PermissionDenied(
                _("You do not have permission to manage this reviewer profile.")
            )

    def get_queryset(self):
        self.check_permission()
        profile = self.get_reviewer_profile()
        return profile.expertise_set.all()

    def perform_create(self, serializer):
        self.check_permission()
        profile = self.get_reviewer_profile()
        serializer.save(reviewer_profile=profile)


@extend_schema_view(
    list=extend_schema(
        operation_id="nested_reviewer_profile_publications_list",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    create=extend_schema(
        operation_id="nested_reviewer_profile_publications_create",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    retrieve=extend_schema(
        operation_id="nested_reviewer_profile_publications_retrieve",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    update=extend_schema(
        operation_id="nested_reviewer_profile_publications_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    partial_update=extend_schema(
        operation_id="nested_reviewer_profile_publications_partial_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    destroy=extend_schema(
        operation_id="nested_reviewer_profile_publications_destroy",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
)
class ReviewerProfilePublicationViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for managing reviewer profile publications."""

    lookup_field = "uuid"
    queryset = models.ReviewerPublication.objects.all()
    serializer_class = serializers.ReviewerPublicationSerializer
    filter_backends = (DjangoFilterBackend,)

    access_declarations = REVIEWER_PROFILE_PART_ACCESS

    def get_reviewer_profile(self):
        profile_uuid = self.kwargs.get("reviewer_profile_uuid")
        return get_object_or_404(models.ReviewerProfile, uuid=profile_uuid)

    def check_permission(self):
        profile = self.get_reviewer_profile()
        user = self.request.user
        # Only profile owner or staff can manage publications
        if not (has_staff_access(self.request) or profile.user == user):
            raise exceptions.PermissionDenied(
                _("You do not have permission to manage this reviewer profile.")
            )

    def get_queryset(self):
        self.check_permission()
        profile = self.get_reviewer_profile()
        return profile.publications.all()

    def perform_create(self, serializer):
        self.check_permission()
        profile = self.get_reviewer_profile()
        serializer.save(reviewer_profile=profile)


# =============================================================================
# Assignment Batch ViewSets (Stage 2 - Proposal Assignment Workflow)
# =============================================================================


class AssignmentBatchViewSet(ActionsViewSet):
    """
    ViewSet for managing assignment batches.

    Assignment batches are created when a call manager generates assignments
    for reviewers. Each batch contains one or more proposal assignments for
    a single reviewer.
    """

    lookup_field = "uuid"
    queryset = models.AssignmentBatch.objects.all().order_by("-created")
    serializer_class = serializers.AssignmentBatchSerializer
    filterset_class = filters.AssignmentBatchFilter

    # Permissions for custom actions - managers only
    send_permissions = cancel_permissions = extend_deadline_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]
    # Batches come from the call's generate_assignments / create_manual_assignment;
    # POSTing here only ever raised an IntegrityError. create_permissions is
    # emptied because the gate below needs an object and a list route has none.
    disabled_actions = ["create"]
    create_permissions = []
    # Editing or deleting a batch is call-management work. Deleting was ungated,
    # so a reviewer could erase an assignment instead of declining it.
    unsafe_methods_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]

    def get_serializer_class(self):
        if self.action == "list":
            return serializers.AssignmentBatchListSerializer
        return serializers.AssignmentBatchSerializer

    @access.shows(
        "Batches sent to the caller as a reviewer, and every batch of calls on "
        "which they hold the call manager role; users with the staff or support "
        "flag and holders of a site-wide CALL.LIST grant see all."
    )
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support or sees_every_call(user):
            return queryset

        # Filter based on user's roles
        return queryset.filter(models.filter_assignment_batches(user))

    @extend_schema(
        description="Send this assignment batch invitation to the reviewer.",
        request=serializers.SendAssignmentBatchSerializer,
        responses={200: serializers.SendAssignmentBatchResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def send(self, request, uuid=None):
        """Send the assignment batch invitation to the reviewer."""
        batch: models.AssignmentBatch = self.get_object()

        if batch.status != models.AssignmentBatchStatuses.DRAFT:
            return response.Response(
                {"error": _("Only draft batches can be sent.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.SendAssignmentBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("manager_notes"):
            batch.manager_notes = serializer.validated_data["manager_notes"]
            batch.save(update_fields=["manager_notes"])

        batch.send_invitation(user=request.user)
        tasks.send_assignment_batch_invitation.delay(batch.uuid)

        return response.Response(
            {
                "detail": _("Assignment batch invitation sent successfully."),
                "expires_at": batch.expires_at,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Cancel this assignment batch.",
        request=None,
        responses={200: serializers.MessageResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        """Cancel the assignment batch."""
        batch: models.AssignmentBatch = self.get_object()

        if batch.status not in [
            models.AssignmentBatchStatuses.DRAFT,
            models.AssignmentBatchStatuses.SENT,
        ]:
            return response.Response(
                {"error": _("Only draft or sent batches can be canceled.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.status = models.AssignmentBatchStatuses.CANCELLED
        batch.save(update_fields=["status"])

        return response.Response(
            {"message": _("Assignment batch canceled.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Extend or modify the expiration date for an assignment batch. "
        "Can reactivate expired batches by setting a future deadline.",
        request=serializers.ExtendDeadlineRequestSerializer,
        responses={200: serializers.ExtendDeadlineResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="extend-deadline")
    def extend_deadline(self, request, uuid=None):
        """
        Extend or modify the expiration date for an assignment batch.

        This allows call managers to:
        - Extend the deadline for sent batches before they expire
        - Reactivate expired batches by setting a new future deadline

        If a batch is in EXPIRED status and a future deadline is set,
        the batch will be reactivated to SENT status.
        """
        batch: models.AssignmentBatch = self.get_object()

        if batch.status not in [
            models.AssignmentBatchStatuses.SENT,
            models.AssignmentBatchStatuses.EXPIRED,
        ]:
            return response.Response(
                {"error": _("Can only extend deadline for sent or expired batches.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ExtendDeadlineRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_expires_at = serializer.validated_data["expires_at"]
        batch.expires_at = new_expires_at
        # A new deadline earns a new reminder before it.
        batch.reminder_sent = False

        # Reactivate expired batch if new deadline is in future
        if batch.status == models.AssignmentBatchStatuses.EXPIRED:
            batch.status = models.AssignmentBatchStatuses.SENT
            batch.manager_notified = False  # Reset notification flag
            # Also reactivate pending items
            batch.items.filter(status=models.AssignmentItemStatuses.EXPIRED).update(
                status=models.AssignmentItemStatuses.PENDING
            )

        batch.save(
            update_fields=["expires_at", "status", "manager_notified", "reminder_sent"]
        )

        # Accepted reviews need no update: Review.review_end_date is never
        # earlier than the deadline of the batch the review was accepted from,
        # so the expiry task already honours the new deadline.

        return response.Response(
            {
                "expires_at": batch.expires_at,
                "status": batch.status,
            },
            status=status.HTTP_200_OK,
        )


class AssignmentItemViewSet(ActionsViewSet):
    """
    ViewSet for managing individual assignment items.

    Each item represents a proposal assignment within a batch.
    Reviewers can accept or decline items individually.
    """

    lookup_field = "uuid"
    queryset = models.AssignmentItem.objects.all().order_by("-created")
    serializer_class = serializers.AssignmentItemSerializer
    filterset_class = filters.AssignmentItemFilter

    # Permissions for manager-only actions
    suggest_alternatives_permissions = reassign_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["batch.call", "batch.call.manager"],
        )
    ]
    # Responding is the reviewer's own consent; managers intervene through
    # reassign / force_accept.
    accept_permissions = decline_permissions = [
        proposal_permissions.user_is_assignment_reviewer
    ]
    # Items come from the call's assignment generation, same as their batch.
    disabled_actions = ["create"]
    create_permissions = []
    # Every other write is call-management work; accept/decline above override
    # this for the reviewer's own response.
    unsafe_methods_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["batch.call", "batch.call.manager"],
        )
    ]

    @access.shows(
        "Items of batches sent to the caller as a reviewer, and every item of "
        "calls on which they hold the call manager role; users with the staff "
        "flag see all."
    )
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff:
            return queryset

        # Filter based on user's roles
        return queryset.filter(models.filter_assignment_items(user))

    @extend_schema(
        description="Accept this assignment item. Creates a Review record.",
        request=serializers.AssignmentItemAcceptSerializer,
        responses={200: serializers.AssignmentItemResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        """Accept the assignment and create a Review record."""
        item: models.AssignmentItem = self.get_object()

        with transaction.atomic():
            # Locked, so two concurrent answers cannot both find it pending
            item = models.AssignmentItem.objects.select_for_update().get(pk=item.pk)

            # Block responses to expired batches
            if item.batch.is_expired:
                raise exceptions.ValidationError(
                    expired_response_error(_("assignment"), item.expires_at)
                )

            review = item.accept(user=request.user)
            activity.log_review_event(review, EventType.REVIEW_ACCEPTED)

        return response.Response(
            {
                "detail": _("Assignment accepted. Review created."),
                "review_uuid": str(review.uuid),
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Decline this assignment item.",
        request=serializers.AssignmentItemDeclineSerializer,
        responses={200: serializers.AssignmentItemResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        """Decline the assignment with an optional reason."""
        item: models.AssignmentItem = self.get_object()

        serializer = serializers.AssignmentItemDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            # Locked, so two concurrent answers cannot both find it pending
            item = models.AssignmentItem.objects.select_for_update().get(pk=item.pk)

            # Block responses to expired batches
            if item.batch.is_expired:
                raise exceptions.ValidationError(
                    expired_response_error(_("assignment"), item.expires_at)
                )

            item.decline(
                reason=serializer.validated_data.get("reason", ""),
                user=request.user,
            )
            activity.log_review_declined(item)

        return response.Response(
            {
                "detail": _("Assignment declined."),
                "review_uuid": None,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Suggest alternative reviewers for a declined assignment.",
        request=None,
        responses={200: serializers.SuggestAlternativeReviewersSerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def suggest_alternatives(self, request, uuid=None):
        """Get alternative reviewer suggestions for a declined item."""
        item: models.AssignmentItem = self.get_object()

        if item.status != models.AssignmentItemStatuses.DECLINED:
            return response.Response(
                {"error": _("Alternatives can only be suggested for declined items.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call = item.batch.call
        proposal = item.proposal

        # Get pool members who:
        # 1. Have accepted invitation
        # 2. Don't already have this proposal assigned
        # 3. Don't have blocking COI
        # 4. Haven't declined this proposal before
        existing_reviewers = models.AssignmentItem.objects.filter(
            proposal=proposal,
        ).values_list("batch__reviewer_pool_entry_id", flat=True)

        declined_reviewers = models.AssignmentItem.objects.filter(
            proposal=proposal,
            status=models.AssignmentItemStatuses.DECLINED,
        ).values_list("batch__reviewer_pool_entry_id", flat=True)

        pool_entries = (
            models.CallReviewerPool.objects.eligible()
            .filter(call=call)
            .exclude(id__in=existing_reviewers)
            .exclude(id__in=declined_reviewers)
            .with_open_assignments()
        )

        # Get affinity scores for suggestions
        suggestions = []
        for entry in pool_entries[:10]:  # Limit to 10 suggestions
            affinity = models.ReviewerProposalAffinity.objects.filter(
                reviewer=entry.reviewer,
                proposal=proposal,
            ).first()

            # Check COI
            has_coi = models.ConflictOfInterest.objects.filter(
                reviewer=entry.reviewer,
                proposal=proposal,
                status__in=["pending", "confirmed"],
            ).exists()

            if has_coi:
                continue

            suggestions.append(
                {
                    "pool_entry_uuid": str(entry.uuid),
                    "reviewer_name": entry.reviewer.user.full_name
                    if entry.reviewer
                    else entry.invited_email,
                    "reviewer_email": entry.reviewer.user.email
                    if entry.reviewer
                    else entry.invited_email,
                    "affinity_score": affinity.affinity_score if affinity else None,
                    "current_assignments": entry.open_assignments,
                    "max_assignments": entry.max_assignments,
                }
            )

        # Sort by affinity score
        suggestions.sort(key=lambda x: x.get("affinity_score") or 0, reverse=True)

        return response.Response(
            {"suggestions": suggestions},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Reassign this item to a different reviewer.",
        request=serializers.ReassignItemSerializer,
        responses={200: serializers.ReassignItemResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def reassign(self, request, uuid=None):
        """Reassign a declined item to a different reviewer."""
        item: models.AssignmentItem = self.get_object()

        if item.status not in [
            models.AssignmentItemStatuses.DECLINED,
            models.AssignmentItemStatuses.EXPIRED,
        ]:
            return response.Response(
                {"error": _("Only declined or expired items can be reassigned.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ReassignItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pool_entry_uuid = serializer.validated_data["reviewer_pool_entry_uuid"]
        manager_notes = serializer.validated_data.get("manager_notes", "")

        try:
            new_pool_entry = models.CallReviewerPool.objects.eligible().get(
                uuid=pool_entry_uuid,
                call=item.batch.call,
            )
        except models.CallReviewerPool.DoesNotExist:
            return response.Response(
                {"error": _("Pool entry not found or not eligible for assignments.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if this reviewer already has this proposal assigned
        existing = models.AssignmentItem.objects.filter(
            batch__reviewer_pool_entry=new_pool_entry,
            proposal=item.proposal,
        ).exists()

        if existing:
            return response.Response(
                {"error": _("This reviewer already has this proposal assigned.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Find or create a draft batch for the new reviewer
        # Only reuse MANUAL draft batches to avoid bundling reassignments
        # with algorithm-generated batches pending approval
        batch, created = models.AssignmentBatch.objects.get_or_create(
            call=item.batch.call,
            reviewer_pool_entry=new_pool_entry,
            status=models.AssignmentBatchStatuses.DRAFT,
            source=models.AssignmentSources.MANUAL,
            defaults={
                "created_by": request.user,
                "manager_notes": manager_notes,
            },
        )

        # Get affinity score
        affinity = models.ReviewerProposalAffinity.objects.filter(
            reviewer=new_pool_entry.reviewer,
            proposal=item.proposal,
        ).first()

        # Check for blocking COI
        coi_records = models.ConflictOfInterest.objects.filter(
            reviewer=new_pool_entry.reviewer,
            proposal=item.proposal,
            status__in=["pending", "recused"],
        )
        has_coi = coi_records.exists()

        # Create new assignment item
        new_item = models.AssignmentItem.objects.create(
            batch=batch,
            proposal=item.proposal,
            affinity_score=affinity.affinity_score if affinity else None,
            reassigned_from=item,
            reassign_count=item.reassign_count + 1,
            has_coi=has_coi,
            status=models.AssignmentItemStatuses.COI_BLOCKED
            if has_coi
            else models.AssignmentItemStatuses.PENDING,
        )
        if has_coi:
            new_item.coi_records.set(coi_records)

        # Mark original item as reassigned
        item.status = models.AssignmentItemStatuses.REASSIGNED
        item.save(update_fields=["status"])

        return response.Response(
            {
                "detail": _("Assignment reassigned successfully."),
                "new_item_uuid": str(new_item.uuid),
                "new_batch_uuid": str(batch.uuid),
                "has_coi": has_coi,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Force-unblock a COI-blocked assignment item (manager override).",
        request=serializers.ForceUnblockSerializer,
        responses={200: serializers.AssignmentItemSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="force-unblock")
    def force_unblock(self, request, uuid=None):
        """Force-unblock a COI-blocked assignment item with a reason."""
        item: models.AssignmentItem = self.get_object()

        if item.status != models.AssignmentItemStatuses.COI_BLOCKED:
            return response.Response(
                {"error": _("Only COI-blocked items can be force-unblocked.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ForceUnblockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        item.status = models.AssignmentItemStatuses.PENDING
        item.has_coi = False
        item.override_reason = serializer.validated_data["override_reason"]
        item.overridden_by = request.user
        item.overridden_at = timezone.now()
        item.save(
            update_fields=[
                "status",
                "has_coi",
                "override_reason",
                "overridden_by",
                "overridden_at",
            ]
        )
        call = item.batch.call
        event_logger.emit(
            "%(user)s lifted the conflict of interest block on the assignment of "
            "%(proposal)s to %(reviewer)s: %(reason)s"
            % {
                "user": request.user.full_name or request.user.username,
                "proposal": item.proposal.name,
                "reviewer": (
                    entry.reviewer.user.full_name
                    if (entry := item.batch.reviewer_pool_entry).reviewer
                    else entry.invited_email
                ),
                "reason": item.override_reason,
            },
            event_type=EventType.REVIEW_ASSIGNMENT_FORCE_UNBLOCKED,
            event_context={
                "call": call,
                "proposal": item.proposal,
                "reason": item.override_reason,
            },
            scopes=[_get_customer(call)],
        )

        return response.Response(
            serializers.AssignmentItemSerializer(
                item, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    force_unblock_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["batch.call", "batch.call.manager"],
        )
    ]


class CallAssignmentConfigurationViewSet(ActionsViewSet):
    """
    ViewSet for managing call assignment configuration.
    """

    lookup_field = "uuid"
    queryset = models.CallAssignmentConfiguration.objects.all()
    serializer_class = serializers.CallAssignmentConfigurationSerializer
    # Visibility below is any role on the call, reviewers and panel members
    # included, so writes cannot ride on it: they need MANAGE_PROPOSAL_REVIEW.
    unsafe_methods_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]
    # The serializer's `call` is read-only, so a POST could never name the call
    # it configures and died on the NOT NULL constraint; the list route also
    # has no object to check the permission against. Refuse it outright.
    disabled_actions = ["create"]

    @access.shows(
        "Configurations of calls on which the caller holds any role; users with "
        "the staff or support flag and holders of a site-wide CALL.LIST grant "
        "see all."
    )
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support or sees_every_call(user):
            return queryset

        connected_calls = get_connected_calls(user)
        return queryset.filter(call__in=connected_calls)

    def perform_update(self, serializer):
        configuration = serializer.save()
        workflow_versions.record_on_commit(configuration.call, self.request.user)


@extend_schema_view(
    retrieve=extend_schema(
        description="Get details of a specific assignment batch with items.",
        responses={200: serializers.MyAssignmentBatchDetailSerializer},
        parameters=[
            OpenApiParameter(
                "uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the assignment batch",
            ),
        ],
    ),
)
class MyAssignmentBatchViewSet(viewsets.ViewSet):
    """
    ViewSet for reviewers to view and respond to their assignment batches.

    This endpoint provides a reviewer-centric view of pending assignments.
    """

    lookup_field = "uuid"
    permission_classes = [rf_permissions.IsAuthenticated]

    access_declarations = {
        "list": [access.owned("Assignment batches sent to the caller as a reviewer.")],
        "retrieve": [
            access.owned("An assignment batch sent to the caller as a reviewer.")
        ],
    }

    @staticmethod
    def _serialize_batch(batch: models.AssignmentBatch) -> dict:
        return {
            "uuid": batch.uuid,
            "call_uuid": batch.call.uuid,
            "call_name": batch.call.name,
            "status": batch.effective_status,
            "status_display": batch.get_effective_status_display(),
            "sent_at": batch.sent_at,
            "expires_at": batch.expires_at,
            "is_expired": batch.is_expired,
            "items_count": batch.items.count(),
            "items_pending_count": batch.items_pending_count,
            "manager_notes": batch.manager_notes,
        }

    @extend_schema(
        description=(
            "List the authenticated reviewer's assignment batches. "
            "By default only batches still awaiting a response are returned. "
            "The status filter uses the effective status: a sent batch past "
            "its deadline is expired even before the expiry task has run."
        ),
        parameters=[
            OpenApiParameter(
                "status",
                {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(models.AssignmentBatchStatuses.SENT_TO_REVIEWER),
                    },
                },
                location=OpenApiParameter.QUERY,
                explode=True,
                description="Effective batch status. Defaults to sent.",
            ),
        ],
        responses={200: serializers.MyAssignmentBatchSerializer(many=True)},
    )
    def list(self, request):
        """List assignment batches sent to the current user."""
        user = request.user

        statuses = request.query_params.getlist("status") or [
            models.AssignmentBatchStatuses.SENT
        ]
        unknown = set(statuses) - set(models.AssignmentBatchStatuses.SENT_TO_REVIEWER)
        if unknown:
            raise exceptions.ValidationError(
                {"status": _("Unknown status: %s.") % ", ".join(sorted(unknown))}
            )

        # Find reviewer profile
        try:
            profile = models.ReviewerProfile.objects.get(user=user)
        except models.ReviewerProfile.DoesNotExist:
            return response.Response([])

        batches = (
            models.AssignmentBatch.objects.filter(
                assignment_batch_status_q(statuses),
                reviewer_pool_entry__reviewer=profile,
                status__in=models.AssignmentBatchStatuses.SENT_TO_REVIEWER,
            )
            .select_related("call")
            .order_by("-sent_at")
        )

        return response.Response([self._serialize_batch(batch) for batch in batches])

    def retrieve(self, request, uuid=None):
        """Get batch details with items for the current user."""
        user = request.user

        try:
            profile = models.ReviewerProfile.objects.get(user=user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.NotFound(_("Reviewer profile not found."))

        # Drafts and cancelled batches were never sent to the reviewer.
        batch = get_object_or_404(
            models.AssignmentBatch,
            uuid=uuid,
            reviewer_pool_entry__reviewer=profile,
            status__in=models.AssignmentBatchStatuses.SENT_TO_REVIEWER,
        )

        disclosure = utils.proposal_disclosure_for_reviewer(batch.call)
        items = []
        for item in batch.items.select_related("proposal", "batch"):
            items.append(
                {
                    "uuid": item.uuid,
                    **utils.disclosed_proposal_fields(item, disclosure),
                    "status": item.effective_status,
                    "status_display": item.get_effective_status_display(),
                    "expires_at": item.expires_at,
                    "is_expired": item.is_expired,
                    "affinity_score": item.affinity_score,
                    "has_coi": item.has_coi,
                }
            )

        return response.Response({**self._serialize_batch(batch), "items": items})


# Who reads and writes a round's ranking and meeting notes, as
# waldur_mastermind.proposal.rankings decides it.
ROUNDS_READ = (
    "rounds of calls they manage (UPDATE_CALL on the call or its managing "
    "organisation) or sit on the panel of, and rounds with published results "
    "of calls whose results they read (CALL.VIEW_RESULTS on the managing "
    "customer) -- every round with the support flag -- but no round with a "
    "proposal they are on the team of or in conflict with"
)
REPORTS_READ = (
    "with the support flag, every one; otherwise those they wrote, those of "
    "calls they manage and those a later step they work on reads, when they "
    "cover no proposal they are on the team of or in conflict with"
)
WRITES_ROUND = (
    "Users with the staff flag, and the call's managers (UPDATE_CALL on the "
    "call or its managing organisation) who read the round."
)
WRITES_RANKING = (
    "The round's overall ranking: users with the staff flag, and the call's "
    "managers (UPDATE_CALL on the call or its managing organisation), panel "
    "chair and vice-chair who read the round. A round report's ranking: the "
    "report's author while it is a draft and they still write for it. Nobody "
    "ranks a proposal they are on the team of."
)


class ProposalRankingViewSet(ActionsViewSet):
    """A round's ranking: where each proposal was placed, its final score and
    the resources recommended for it.

    Written by the call's managers until the round goes to its decision;
    read by them and the call's panel members. Applicants and reviewers
    read none of it, and nobody but staff reads the ranking of a proposal
    they are on the team of.
    """

    lookup_field = "uuid"
    serializer_class = serializers.ProposalRankingSerializer
    filterset_class = filters.ProposalRankingFilter
    filter_backends = (DjangoFilterBackend,)
    disabled_actions = ["update"]

    # Checked by the serializer (create, partial_update), perform_destroy
    # and the import itself.
    access_declarations = {
        "create": [access.custom(WRITES_RANKING)],
        "partial_update": [access.custom(WRITES_RANKING)],
        "destroy": [access.custom(WRITES_RANKING)],
        "import_csv": [access.custom(WRITES_RANKING)],
        "export_csv": [
            access.custom(
                "Whoever reads the round's ranking, or the named round report; "
                "the file holds the rankings they read, and for those who write "
                "the ranking the proposals not ranked yet."
            )
        ],
    }

    @access.shows(
        "Users with the staff flag read every ranking. Others read the overall "
        f"ranking of {ROUNDS_READ}; and the ranking within the round reports "
        f"they read: {REPORTS_READ}."
    )
    def get_queryset(self):
        queryset = rankings.readable_rankings(self.request.user)
        if self.action == "list" and not self.request.query_params.get(
            "meeting_note_uuid"
        ):
            # A list is the round's overall ranking unless it names a report.
            queryset = queryset.filter(meeting_note__isnull=True)
        return queryset

    @staticmethod
    def _lock_report_draft(note):
        """Lock a round report's row and refuse it once submitted, so a
        ranking write and the report's submission serialise."""
        if note is None:
            return
        note = models.RoundMeetingNote.objects.select_for_update().get(pk=note.pk)
        if note.submitted_at is not None:
            raise exceptions.ValidationError(
                _("The round report is submitted; its ranking is fixed.")
            )

    def perform_create(self, serializer):
        call_round = serializer.validated_data["round"]
        note = serializer.validated_data.get("meeting_note")
        with transaction.atomic():
            # Serialise ranking writes per round; the rank is checked again
            # under the lock.
            locked_round = rankings.lock_round(call_round)
            self._lock_report_draft(note)
            rankings.ensure_ranking_open(locked_round)
            rankings.ensure_rank_free(
                locked_round, serializer.validated_data["rank"], meeting_note=note
            )
            rankings.save_guarded(serializer.save)

    def perform_update(self, serializer):
        instance = serializer.instance
        with transaction.atomic():
            locked_round = rankings.lock_round(instance.round)
            self._lock_report_draft(instance.meeting_note)
            rankings.ensure_ranking_open(locked_round)
            if "rank" in serializer.validated_data:
                rankings.ensure_rank_free(
                    locked_round,
                    serializer.validated_data["rank"],
                    exclude_pk=instance.pk,
                    meeting_note=instance.meeting_note,
                )
            rankings.save_guarded(serializer.save)

    def perform_destroy(self, instance):
        rankings.ensure_can_write_ranking(
            self.request.user, instance.round, instance.meeting_note
        )
        rankings.ensure_not_on_team(self.request.user, instance.proposal)
        with transaction.atomic():
            locked_round = rankings.lock_round(instance.round)
            self._lock_report_draft(instance.meeting_note)
            rankings.ensure_ranking_open(locked_round)
            instance.delete()

    def _readable_report(self, call_round, note_uuid):
        if not note_uuid:
            return None
        try:
            return rankings.readable_meeting_notes(self.request.user).get(
                uuid=note_uuid, round=call_round, call_step__isnull=False
            )
        except (
            models.RoundMeetingNote.DoesNotExist,
            DjangoValidationError,
            ValueError,
        ):
            raise exceptions.NotFound()

    def _readable_round(self, round_uuid):
        if not round_uuid:
            raise exceptions.ValidationError(
                {"round_uuid": _("The round_uuid query parameter is required.")}
            )
        try:
            return rankings.readable_rounds(self.request.user).get(uuid=round_uuid)
        except (models.Round.DoesNotExist, DjangoValidationError, ValueError):
            raise exceptions.NotFound()

    @extend_schema(
        description=(
            "The round's ranking as CSV: one row per ranked proposal, with a "
            "recommended:<requested offering uuid>:<component type> column per "
            "offering component of the call. For the call's managers, the "
            "proposals of the round not ranked yet follow with empty values, "
            "so the file can be filled in and imported."
        ),
        parameters=[
            OpenApiParameter(
                "round_uuid",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=True,
                description="UUID of the round.",
            ),
            OpenApiParameter(
                "meeting_note_uuid",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=False,
                description=(
                    "UUID of a round report: its ranking instead of the "
                    "round's overall one."
                ),
            ),
        ],
        responses={(200, "text/csv"): OpenApiTypes.STR},
        filters=False,
    )
    @no_count_action
    @decorators.action(detail=False, methods=["get"])
    def export_csv(self, request):
        call_round = self._readable_round(request.query_params.get("round_uuid"))
        note = self._readable_report(
            call_round, request.query_params.get("meeting_note_uuid")
        )
        ranked = list(
            rankings.readable_rankings(request.user)
            .filter(round=call_round, meeting_note=note)
            .order_by("rank", "id")
        )
        unranked = []
        if rankings.can_write_ranking(request.user, call_round, note):
            candidates = (
                note.proposals.all()
                if note is not None
                else models.Proposal.objects.filter(round=call_round)
            )
            unranked = (
                candidates.filter(state__in=rankings.RANKABLE_STATES)
                .exclude(rankings__round=call_round, rankings__meeting_note=note)
                .order_by("name", "id")
            )
        content = rankings.export_csv(call_round, ranked, unranked)
        result = HttpResponse(content, content_type="text/csv")
        result["Content-Disposition"] = (
            f'attachment; filename="ranking-{call_round.slug}.csv"'
        )
        return result

    @extend_schema(
        request={"multipart/form-data": serializers.RankingImportSerializer},
        responses={200: serializers.RankingImportResponseSerializer},
        description=(
            "Create or update the round's ranking from a CSV in the export's "
            "format. Rows without a rank are left out; rankings not in the "
            "file stay as they are. Ranks are checked across the round as it "
            "will be after the import, so a file can reorder it. All or "
            "nothing: an error in any row saves no row. Refused for a file "
            "over 1 MB, or with more rows than the round has proposals and "
            "a few more."
        ),
    )
    @decorators.action(detail=False, methods=["post"])
    def import_csv(self, request):
        serializer = serializers.RankingImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        call_round = serializer.validated_data["round"]
        note = serializer.validated_data.get("meeting_note")
        if note is not None:
            note = self._readable_report(call_round, note.uuid.hex)
        elif (
            not rankings.readable_rounds(request.user).filter(pk=call_round.pk).exists()
        ):
            raise exceptions.NotFound()
        rankings.ensure_can_write_ranking(request.user, call_round, note)
        rankings.ensure_ranking_open(call_round)
        # Read no further than the limit, whatever size the upload claims.
        raw = serializer.validated_data["file"].read(rankings.MAX_IMPORT_BYTES + 1)
        if len(raw) > rankings.MAX_IMPORT_BYTES:
            raise exceptions.ValidationError(
                {
                    "file": _("The file is larger than %(max)s bytes.")
                    % {"max": rankings.MAX_IMPORT_BYTES}
                }
            )
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise exceptions.ValidationError({"file": _("The file is not UTF-8 text.")})
        rows = rankings.parse_csv(call_round, content, note, request.user)
        with transaction.atomic():
            self._lock_report_draft(note)
            created, updated = rankings.import_rows(
                call_round, rows, request.user, note
            )
        return response.Response(
            serializers.RankingImportResponseSerializer(
                {"created": created, "updated": updated}
            ).data,
            status=status.HTTP_200_OK,
        )

    import_csv_serializer_class = serializers.RankingImportSerializer


@extend_schema_view(
    create=extend_schema(
        request={"multipart/form-data": serializers.RoundMeetingNoteSerializer}
    ),
    partial_update=extend_schema(
        request={"multipart/form-data": serializers.RoundMeetingNoteSerializer}
    ),
)
class RoundMeetingNoteViewSet(UserChecklistMixin, ActionsViewSet):
    """Committee and panel feedback recorded per round, with its documents.

    A free note is written by the call's managers and read by them and the
    call's panel members. A round report -- the note of a step run once per
    round -- is created when the step is opened, written and submitted by
    its author, answering the step's form, and read as ``rankings`` says.
    """

    lookup_field = "uuid"
    serializer_class = serializers.RoundMeetingNoteSerializer
    filterset_class = filters.RoundMeetingNoteFilter
    filter_backends = (DjangoFilterBackend,)
    # A report's form is its step's; there is no form for a new note.
    disabled_actions = ["update", "checklist_template"]

    # Both read the form of a note get_object() found, and check nothing more.
    access_filtered_actions = ("checklist", "completion_status")
    # Checked by the serializer (create, partial_update), perform_destroy,
    # the action body and round_steps.
    access_declarations = {
        "create": [access.custom(WRITES_ROUND)],
        "partial_update": [
            access.custom(
                f"A free note: {WRITES_ROUND} A round report: its author while "
                "it is a draft and they still write for it."
            )
        ],
        "destroy": [access.custom(WRITES_ROUND)],
        "submit_answers": [
            access.custom(
                "A round report's author while it is a draft and they still "
                "write for it."
            )
        ],
        "submit": [
            access.custom(
                "A round report's author while they still write for it: still "
                "one of the authors of a proposal it covers."
            )
        ],
        "reopen": [access.custom(WRITES_ROUND)],
    }

    @access.shows(
        "Users with the staff flag read every note. Others read the free notes "
        f"of {ROUNDS_READ}; and the round reports they read: {REPORTS_READ}."
    )
    def get_queryset(self):
        return rankings.readable_meeting_notes(self.request.user).prefetch_related(
            "documents", "proposals"
        )

    def perform_destroy(self, instance):
        if instance.is_report:
            # A report is the record of its step; only one that covers
            # nothing, its author no longer writing for the step, may go.
            rankings.ensure_can_write(self.request.user, instance.round)
            if instance.submitted_at is not None or instance.proposals.exists():
                raise exceptions.ValidationError(
                    _("A round report that covers proposals cannot be deleted.")
                )
        else:
            rankings.ensure_can_write(self.request.user, instance.round)
        instance.delete()

    def get_checklist_completion(self, obj):
        return round_steps.report_completion(obj)

    @extend_schema(
        description=(
            "Answer the round report's form, as its author, until the report "
            "is submitted."
        ),
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={200: checklist_serializers.AnswerSubmitResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def submit_answers(self, request, uuid=None):
        note = self.get_object()
        if not note.is_report or not rankings.can_write_note(request.user, note):
            raise exceptions.PermissionDenied(
                _(
                    "Only the author of a round report answers its form, until "
                    "the report is submitted."
                )
            )
        return super().submit_answers(request, uuid)

    # Whoever reads the report reads its form; get_object holds that.
    checklist_permissions = []
    completion_status_permissions = []
    submit_answers_permissions = []

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.SubmitRoundReportResponseSerializer},
        description=(
            "Submit a round report, as its author. Its form's required "
            "questions must be answered. What it covers, and its ranking, are "
            "then fixed. Each covered proposal whose round reports are all "
            "submitted has the step completed (assessment steps), listed in "
            "completed; one that cannot be is listed in failed."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        note, completed, failed = round_steps.submit_report(
            self.get_object(), request.user
        )
        note.refresh_from_db()
        return response.Response(
            serializers.SubmitRoundReportResponseSerializer(
                {"report": note, "completed": completed, "failed": failed},
                context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_200_OK,
        )

    submit_permissions = []
    non_atomic_actions = ("submit",)

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.RoundMeetingNoteSerializer},
        description=(
            "Hand a submitted round report back to its author, for the "
            "call's managers, while proposals stand at its step: a proposal "
            "that reached the step after the report was submitted then joins "
            "it."
        ),
    )
    @decorators.action(detail=True, methods=["post"])
    def reopen(self, request, uuid=None):
        note = round_steps.reopen_report(self.get_object(), request.user)
        note.refresh_from_db()
        return response.Response(
            serializers.RoundMeetingNoteSerializer(
                note, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    reopen_permissions = []


class MyTaskViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """The open workflow work of the current user, across every call.

    Each row is something the user must do next: a contribution to fill, a
    step to complete, a review to write, an assignment or invitation to
    answer, an applicant response or award to give, a conflict of interest
    to rule out, a report to release or a workflow to advance. Rows follow
    the rules that gate acting on them, and name only what the user can
    already open. Soonest due first.
    """

    serializer_class = serializers.MyTaskSerializer
    permission_classes = (rf_permissions.IsAuthenticated,)
    filter_backends = ()

    access_declarations = {
        "list": [access.owned("The caller's own open workflow tasks.")],
        "summary": [access.owned("The caller's own open workflow tasks.")],
    }

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "offering_uuid",
                OpenApiTypes.UUID,
                description="Only the tasks about this offering.",
                extensions={
                    "x-waldur-operation-id": "marketplace_provider_offerings_retrieve"
                },
            ),
            OpenApiParameter(
                "provider_uuid",
                OpenApiTypes.UUID,
                description=(
                    "Only the tasks about an offering of this provider organization."
                ),
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
            OpenApiParameter(
                "due",
                OpenApiTypes.STR,
                enum=[choice for choice, _ in my_tasks.Due.CHOICES],
                description=(
                    "Only the tasks past their deadline (overdue), or due "
                    "within PROPOSAL_DASHBOARD_REVIEWS_DUE_WITHIN_DAYS days "
                    "and not yet past it (due_soon)."
                ),
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def _tasks(self):
        def hex_param(name):
            value = self.request.query_params.get(name) or ""
            return value.replace("-", "").lower() or None

        return my_tasks.tasks_of(
            self.request.user,
            offering_uuid=hex_param("offering_uuid"),
            provider_uuid=hex_param("provider_uuid"),
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return []
        tasks = self._tasks()
        due = self.request.query_params.get("due")
        if due in dict(my_tasks.Due.CHOICES):
            tasks = my_tasks.with_due(tasks, due)
        return tasks

    @extend_schema(
        summary="Summarise the current user's open tasks",
        description=(
            "How many tasks the list holds, and how many of them are overdue "
            "or due soon: the counts the list's ``due`` filter narrows it to. "
            "Takes the list's offering_uuid and provider_uuid."
        ),
        responses={200: serializers.MyTaskSummarySerializer},
    )
    @no_count_action
    @decorators.action(detail=False, methods=["get"])
    def summary(self, request):
        return response.Response(
            serializers.MyTaskSummarySerializer(
                my_tasks.summary_of(self._tasks())
            ).data,
            status=status.HTTP_200_OK,
        )
