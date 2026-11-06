"""Post-award runs of an accepted proposal: extension requests and the final
report.

A run walks the call's steps of one post-award phase (``WorkflowPhases``).
The workflow engine (``workflow_service``) moves through its steps as it does
through the evaluation's, and calls ``close_run`` where the evaluation would
accept, reject or cancel the proposal: a run never does any of those. This
module holds what a run is and does besides walking: when one may be opened,
what an extension asks for and grants, and what its approval changes.
"""

import datetime
import logging
from typing import cast

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import resource_changes
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.managers import get_connected_offerings
from waldur_mastermind.proposal import (
    activity,
    change_requests,
    dry_run_context,
    models,
)
from waldur_mastermind.proposal.enums import (
    ChangeRequestResolutions,
    ContributionOutcomes,
    PostAwardStatuses,
    ProposalRunStates,
    ProposalStates,
    StepContributionStatuses,
    StepKinds,
    WorkflowPhases,
)

logger = logging.getLogger(__name__)


class RunError(ValueError):
    """A run that cannot be opened, changed or closed."""


def _today():
    return timezone.localdate()


def open_run_of(proposal):
    """The proposal's open run, or None."""
    return proposal.runs.filter(state=ProposalRunStates.OPEN).first()


def provisioned_items(proposal):
    """The award's items that provisioned a resource, oldest first."""
    return (
        proposal.awarded_resources.filter(resource__isnull=False)
        .select_related(
            "resource__offering", "resource__project", "requested_offering__offering"
        )
        .order_by("created", "id")
    )


def provisioned_resources(proposal):
    """The resources allocation provisioned for the proposal, by uuid hex."""
    return {
        item.resource.uuid.hex: item.resource for item in provisioned_items(proposal)
    }


def offset_days(proposal, phase):
    """Days before the project's end that time ``phase``: the offset on its
    first enabled step, 0 when it names none."""
    first = models.CallWorkflowStep.phase_steps(proposal.round.call_id, phase).first()
    if first is None:
        return None
    return first.project_end_offset_days or 0


def window_end(proposal, phase=WorkflowPhases.EXTENSION):
    """The last day an extension can be asked for: ``offset`` days before the
    project's end. None when the project has no end date."""
    project = proposal.project
    if project is None or project.end_date is None:
        return None
    return project.end_date - datetime.timedelta(days=offset_days(proposal, phase) or 0)


def final_report_opens_on(proposal):
    """The day the final report opens by itself, or None: never for a
    project without an end date, nor for a call without a closing."""
    project = proposal.project
    offset = offset_days(proposal, WorkflowPhases.CLOSING)
    if project is None or project.end_date is None or offset is None:
        return None
    return project.end_date - datetime.timedelta(days=offset)


def open_refusal(proposal, phase, today=None):
    """Why a run of ``phase`` cannot be opened on ``proposal`` now, or None.

    The proposal is accepted, provisioned and not completed, has no open
    run, and its call has steps for the phase. An extension is asked for
    until the end of its window; a final report is not opened twice once
    it was accepted, which completes the proposal anyway.
    """
    today = today or _today()
    if phase not in WorkflowPhases.POST_AWARD:
        return f"'{phase}' is not a post-award phase."
    if proposal.state != ProposalStates.ACCEPTED or proposal.project_id is None:
        return (
            "Only an accepted proposal whose project was created has post-award steps."
        )
    if proposal.completed_at is not None:
        return "The proposal's project is completed."
    if open_run_of(proposal) is not None:
        return "The proposal has an open extension request or final report."
    if not models.CallWorkflowStep.phase_steps(proposal.round.call_id, phase).exists():
        return f"The call has no {phase} steps."
    if phase == WorkflowPhases.EXTENSION:
        last_day = window_end(proposal)
        if last_day is not None and today > last_day:
            return (
                f"An extension can be requested until {last_day.isoformat()}, "
                "before the project's end."
            )
    return None


def validate_limits(proposal, limits):
    """Check limits asked for or granted, per provisioned resource, and
    return them with the limits the server derives applied.

    Keyed by the uuid of a resource the award provisioned; each value maps
    the offering's components to amounts, as the resource's limits do. A
    resource on a prepaid offering is extended through a renewal instead.
    """
    if limits in (None, {}):
        return {}
    if not isinstance(limits, dict):
        raise serializers.ValidationError("Limits are given per resource uuid.")
    resources = provisioned_resources(proposal)
    cleaned = {}
    for key, value in limits.items():
        resource = resources.get(str(key).replace("-", ""))
        if resource is None:
            raise serializers.ValidationError(
                {key: "Not a resource provisioned for this proposal."}
            )
        if resource.offering.components.filter(is_prepaid=True).exists():
            raise serializers.ValidationError(
                {
                    key: (
                        "The resource is prepaid: it is extended through a "
                        "renewal, not an extension request."
                    )
                }
            )
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                {key: "Limits map the offering's components to amounts."}
            )
        try:
            cleaned[resource.uuid.hex] = marketplace_utils.validate_limits(
                value, resource.offering, resource
            )
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({key: exc.detail})
    return cleaned


def validate_end_date(proposal, end_date):
    """An extension's end date lies ahead, after the project's current end."""
    if end_date is None:
        return None
    _check_extends(proposal.project if proposal.project_id else None, end_date)
    return end_date


def _check_extends(project, end_date):
    """Refuse an end date that does not extend ``project``.

    It must lie ahead and after the project's current end: an extension
    never shortens the allocation. A project without an end date has nothing
    to extend, and an extension never gives it one -- the end date that
    follows would end its resources.
    """
    if end_date <= _today():
        raise serializers.ValidationError("The end date must be in the future.")
    if project is None:
        return
    current = project.end_date
    if current is None:
        raise serializers.ValidationError(
            "The project has no end date: an extension cannot set one."
        )
    if end_date <= current:
        raise serializers.ValidationError(
            f"The end date must be after the project's current end, {current.isoformat()}."
        )


def prefill_granted(run):
    """Start what the extension's decision grants as what was asked, once."""
    if run.phase != WorkflowPhases.EXTENSION:
        return
    if run.granted_end_date is not None or run.granted_limits:
        return
    run.granted_end_date = run.requested_end_date
    run.granted_limits = dict(run.requested_limits or {})
    run.save(update_fields=["granted_end_date", "granted_limits", "modified"])


def assessed_offering_ids(run):
    """The offerings whose teams accepted the extension in the run's
    assessment: an accepted or conditionally accepted contribution written by
    someone acting on the offering as its provider (``get_connected_offerings``),
    and no rejected one. A slot a
    call manager filled in the team's place speaks for the consumer, not the
    provider, and does not count. Only these offerings' resources take the
    granted changes."""
    contributions = models.StepContribution.objects.filter(
        proposal_step__run=run,
        status=StepContributionStatuses.SUBMITTED,
        requested_offering__isnull=False,
    ).select_related("requested_offering", "contributor")
    accepted, rejected = set(), set()
    for contribution in contributions:
        offering_id = contribution.requested_offering.offering_id
        if contribution.outcome in ContributionOutcomes.NEGATIVE:
            rejected.add(offering_id)
            continue
        if contribution.outcome not in (
            ContributionOutcomes.ACCEPTED,
            ContributionOutcomes.CONDITIONALLY_ACCEPTED,
        ):
            continue
        # A role that grants nothing on the offering -- one naming people for
        # work elsewhere -- does not make its holder the provider.
        written_by_team = (
            contribution.contributor is not None
            and get_connected_offerings(contribution.contributor)
            .filter(object_id=offering_id)
            .exists()
        )
        if written_by_team:
            accepted.add(offering_id)
    return accepted - rejected


def apply_extension(proposal, run, decided_by):
    """Carry out a granted extension, inside the decision's transaction.

    The project's end date moves at once, and only later: a grant that would
    shorten the project, or give an end date to a project without one, is
    refused (``_check_extends``). On each provisioned resource whose
    offering's team accepted the extension in the run's assessment
    (``assessed_offering_ids``) the end date moves as well -- prepaid ones
    excepted, which renew instead -- and changed limits become an update
    order, reviewed for the consumer by the decider and left to the
    provider's approval as the offering routes it. A resource whose
    offering's team did not accept the extension is left as it is, whatever
    the offering's change-request options say. What was done and left
    undone is recorded on the run.
    """
    project = cast(
        structure_models.Project,
        structure_models.Project.objects.select_for_update().get(
            pk=proposal.project_id
        ),
    )
    granted_end = run.granted_end_date
    if granted_end is not None:
        # Checked when the grant was drafted, and again here against the
        # project as it stands now: its end may have moved since.
        _check_extends(project, granted_end)
    applied = {
        "project_end_date": None,
        "resource_end_dates": [],
        "orders": [],
        "skipped": [],
    }
    # No end date change request rows are recorded alongside: creating one
    # mails its approvers to review it, and this one is decided already.
    # The run records what changed, and the resources' event logs say who.
    if granted_end is not None and granted_end != project.end_date:
        previous = project.end_date
        project.end_date = granted_end
        if dry_run_context.current() is not None:
            # A dry run's project stands in for the allocation: its end date
            # moves, and nothing that follows a real project's move runs.
            structure_models.Project.objects.filter(pk=project.pk).update(
                end_date=granted_end
            )
        else:
            project.save(update_fields=["end_date"])
        applied["project_end_date"] = {
            "previous": previous.isoformat() if previous else None,
            "end_date": granted_end.isoformat(),
        }

    accepted_offerings = assessed_offering_ids(run)
    granted_limits = run.granted_limits or {}
    for item in provisioned_items(proposal):
        resource = marketplace_models.Resource.objects.select_for_update().get(
            pk=item.resource_id
        )
        key = resource.uuid.hex

        def skip(reason, resource_uuid=key):
            applied["skipped"].append(
                {"resource_uuid": resource_uuid, "reason": reason}
            )

        wants_limits = key in granted_limits and granted_limits[key] != resource.limits
        # A resource's end only ever moves later, as the project's does.
        wants_end = (
            granted_end is not None
            and resource.end_date is not None
            and resource.end_date < granted_end
        )
        if not (wants_limits or wants_end):
            continue
        if resource.offering_id not in accepted_offerings:
            skip("The offering's team did not accept the extension.")
            continue
        if resource.state != marketplace_models.Resource.States.OK:
            skip("The resource is not in OK state.")
            continue
        if wants_end:
            if resource.offering.components.filter(is_prepaid=True).exists():
                skip("The resource is prepaid: it is extended through a renewal.")
            else:
                resource_previous_end = resource.end_date.isoformat()
                resource_changes.apply_end_date(
                    resource,
                    granted_end,
                    decided_by,
                    "End date of marketplace resource %(resource_name)s has been "
                    "updated by an approved extension of its proposal."
                    " End date: %(end_date)s."
                    " User: %(user)s.",
                )
                applied["resource_end_dates"].append(
                    {
                        "resource_uuid": key,
                        "previous": resource_previous_end,
                        "end_date": granted_end.isoformat(),
                    }
                )
        if wants_limits:
            order = resource_changes.create_limit_update_order(
                resource,
                granted_limits[key],
                decided_by,
                consumer_reviewed_by=decided_by,
            )
            applied["orders"].append(
                {"resource_uuid": key, "order_uuid": order.uuid.hex}
            )
    run.applied_changes = applied
    run.save(update_fields=["applied_changes", "modified"])


def close_run(proposal, run, state, user):
    """End ``run`` with ``state``; the proposal stays accepted.

    An approved extension is carried out (``apply_extension``); an approved
    closing completes the proposal. The caller holds the proposal's row lock
    inside a transaction.
    """
    run.state = state
    run.closed_at = timezone.now()
    run.save(update_fields=["state", "closed_at", "modified"])
    # Changes still asked of the team in the run are no longer wanted.
    change_requests.close_open(
        models.ProposalChangeRequest.objects.filter(step_instance__run=run),
        ChangeRequestResolutions.CANCELLED,
        user,
    )
    update_fields = ["current_step"]
    proposal.current_step = None
    if state == ProposalRunStates.APPROVED and run.phase == WorkflowPhases.EXTENSION:
        try:
            apply_extension(proposal, run, user)
        except serializers.ValidationError as exc:
            raise ValueError(f"The extension cannot be applied: {exc.detail}")
    if state == ProposalRunStates.APPROVED and run.phase == WorkflowPhases.CLOSING:
        proposal.completed_at = timezone.now()
        proposal.completed_by = user
        update_fields += ["completed_at", "completed_by"]
    proposal.save(update_fields=update_fields)
    activity.log_run_closed(run)


def due_final_report_proposals(today=None):
    """Accepted proposals whose final report is due and was never opened.

    Not completed, no run open, no closing run yet, and the project's end
    date -- an extension having moved it already -- minus the closing's
    offset reached. A project without an end date never comes due.
    """
    today = today or _today()
    candidates = (
        models.Proposal.objects.filter(
            state=ProposalStates.ACCEPTED,
            completed_at__isnull=True,
            project__end_date__isnull=False,
            round__call__workflow_steps__phase=WorkflowPhases.CLOSING,
            round__call__workflow_steps__is_enabled=True,
        )
        .exclude(
            Exists(
                models.ProposalRun.objects.filter(proposal=OuterRef("pk")).filter(
                    Q(phase=WorkflowPhases.CLOSING) | Q(state=ProposalRunStates.OPEN)
                )
            )
        )
        .select_related("project", "round__call")
        .distinct()
    )
    return [
        proposal
        for proposal in candidates
        if (opens_on := final_report_opens_on(proposal)) is not None
        and opens_on <= today
    ]


def post_award_status(proposal):
    """What the proposal's dashboard says of its post-award work, or None.

    Reads ``_open_run_phase``, ``_current_step_kind`` and
    ``_has_extension_granted`` when the queryset annotated them.
    """
    if proposal.state != ProposalStates.ACCEPTED:
        return None
    if proposal.completed_at is not None:
        return PostAwardStatuses.PROJECT_COMPLETED
    if hasattr(proposal, "_open_run_phase"):
        phase = proposal._open_run_phase
        kind = proposal._current_step_kind
        extended = proposal._has_extension_granted
    else:
        run = open_run_of(proposal)
        phase = run.phase if run else None
        kind = (
            proposal.current_step.call_step.kind if proposal.current_step_id else None
        )
        extended = proposal.runs.filter(
            phase=WorkflowPhases.EXTENSION, state=ProposalRunStates.APPROVED
        ).exists()
    if phase == WorkflowPhases.CLOSING:
        if kind == StepKinds.APPLICANT_ACTION:
            return PostAwardStatuses.PENDING_FINAL_REPORT
        return PostAwardStatuses.FINAL_REPORT_SUBMITTED
    if phase == WorkflowPhases.EXTENSION:
        if kind == StepKinds.APPLICANT_ACTION:
            return PostAwardStatuses.EXTENSION_REQUESTED
        if kind == StepKinds.DECISION:
            return PostAwardStatuses.EXTENSION_PENDING_RESULTS
        return PostAwardStatuses.EXTENSION_IN_ASSESSMENT
    if extended:
        return PostAwardStatuses.PROJECT_EXTENDED
    return None
