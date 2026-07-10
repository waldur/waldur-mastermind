import datetime
import logging
import uuid
from typing import cast

from constance import config
from dateutil.relativedelta import relativedelta
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from rest_framework import serializers

from waldur_core.core.fields import StringUUID
from waldur_core.core.utils import get_system_robot
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import (
    AllocationTimes,
    BulkRoundCadence,
    CallStates,
    RequestedOfferingStates,
)

logger = logging.getLogger(__name__)


def allocate_proposal(proposal: proposal_models.Proposal, approved_by=None):
    # Idempotency guard: a proposal is provisioned exactly once. Without this a
    # second allocation (e.g. re-driving the workflow, or a stale caller) would
    # create a duplicate project + resources + orders. The workflow terminal
    # already checks project_id, but the guard belongs here so no caller can
    # double-provision.
    if proposal.project is not None:
        logger.info(
            "Proposal %s is already allocated to project %s; skipping.",
            proposal.uuid,
            proposal.project,
        )
        return
    proposal_round = proposal.round
    name = proposal.name
    start_date = None
    call_prefix = proposal_round.call.backend_id or proposal_round.call.slug
    project_name = " - ".join(
        [call_prefix, proposal_round.start_time.strftime("%Y-%m-%d"), name]
    )[: structure_models.PROJECT_NAME_LENGTH]

    # Allocation timing is a call-level policy on the allocation_decision step;
    # the concrete date stays per-round.
    allocation_step = proposal_models.CallWorkflowStep.objects.filter(
        call=proposal_round.call, step="allocation_decision"
    ).first()
    allocation_time = (
        allocation_step.allocation_time
        if allocation_step
        else AllocationTimes.ON_DECISION
    )
    if allocation_time == AllocationTimes.FIXED_DATE and proposal_round.allocation_date:
        # Project.start_date is a DateField; the round's allocation_date is a
        # DateTimeField. Coerce to a date so downstream date comparisons (e.g.
        # the order-created notification handler) don't hit a datetime-vs-date
        # TypeError.
        start_date = proposal_round.allocation_date.date()

    project = structure_models.Project.objects.create(
        customer=proposal_round.call.manager.customer,
        name=project_name,
        start_date=start_date,
    )
    project = cast(structure_models.Project, project)

    if start_date:
        logger.info(
            f"Field start_date of {project} has been changed to {proposal.round.allocation_date}."
        )

    proposal.project = project
    if approved_by is not None:
        proposal.approved_by = approved_by
    proposal.save()

    requested_resources = proposal.requestedresource_set.filter(
        requested_offering__state=RequestedOfferingStates.ACCEPTED
    )

    for mapping in proposal.round.call.proposalprojectrolemapping_set.all():  # type: ignore
        users = get_users(proposal, mapping.proposal_role.name)
        for user in users:
            if mapping.project_role:
                project.add_user(user, mapping.project_role)
            else:
                continue

    for requested_resource in requested_resources:
        with transaction.atomic():
            attrs = dict(
                project=project,
                offering=requested_resource.requested_offering.offering,
                plan=requested_resource.requested_offering.plan,
                attributes=requested_resource.attributes,
                limits=requested_resource.limits,
            )
            resource = marketplace_models.Resource(
                **attrs,
                name=project.name,
            )
            resource.init_cost()
            resource.save()

            order = marketplace_models.Order(
                **attrs,
                resource=resource,
                created_by=get_system_robot(),
            )
            order.init_cost()
            order.save()

            requested_resource.resource = resource
            requested_resource.save()


def process_closed_round(call_round: proposal_models.Round):
    """Process a closed round: cancel draft proposals."""
    from waldur_mastermind.proposal.enums import ProposalStates

    call_round.proposal_set.filter(state=ProposalStates.DRAFT).update(
        state=ProposalStates.CANCELED
    )


def get_proposal_review_counts(proposal: proposal_models.Proposal) -> dict:
    base_queryset = proposal_models.Review.objects.filter(proposal=proposal)

    submitted_reviews = base_queryset.filter(
        state=proposal_models.Review.States.SUBMITTED
    ).count()

    rejected_reviews = base_queryset.filter(
        state=proposal_models.Review.States.REJECTED
    ).count()

    pending_reviews = base_queryset.filter(
        state=proposal_models.Review.States.IN_REVIEW,
    ).count()

    return {
        "submitted_reviews": submitted_reviews,
        "rejected_reviews": rejected_reviews,
        "pending_reviews": pending_reviews,
    }


# Fields managed by Django or set explicitly during duplication.
_DUPLICATE_CALL_OVERRIDE_FIELDS = frozenset(
    {"id", "uuid", "slug", "name", "state", "created_by", "created", "modified"}
)

# call/step are the upsert lookup keys; id/uuid/created/modified are
# identity/timestamp fields that must not be carried over from the source row.
_DUPLICATE_WORKFLOW_STEP_OVERRIDE_FIELDS = frozenset(
    {"id", "uuid", "call", "call_id", "step", "created", "modified"}
)

# Sections the user can include in or exclude from a duplicate. Each key maps
# to a default value; the API surface mirrors marketplace offering import.
DUPLICATE_CALL_SECTION_DEFAULTS: dict[str, bool] = {
    "copy_documents": True,
    "copy_offerings": True,
    "copy_rounds": True,
    "copy_workflow_steps": True,
    "copy_resource_templates": True,
    "copy_role_mappings": True,
    "copy_applicant_visibility_config": True,
    "copy_coi_configuration": True,
    "copy_matching_configuration": True,
    "copy_assignment_configuration": True,
}


def _clone_concrete_fields(instance, exclude: frozenset[str]) -> dict:
    return {
        f.attname: getattr(instance, f.attname)
        for f in instance._meta.concrete_fields
        if f.name not in exclude and f.attname not in exclude
    }


def _prepare_clone(instance) -> None:
    """Reset id/pk/uuid on an instance so the next ``save()`` inserts a new row."""
    instance.pk = None
    instance.id = None
    if hasattr(instance, "uuid"):
        instance.uuid = StringUUID(uuid.uuid4().hex)


def _resolve_sections(overrides: dict[str, bool] | None) -> dict[str, bool]:
    sections = dict(DUPLICATE_CALL_SECTION_DEFAULTS)
    if overrides:
        sections.update({k: bool(v) for k, v in overrides.items() if k in sections})
    return sections


@transaction.atomic
def duplicate_call(
    source: proposal_models.Call,
    new_name: str,
    created_by,
    sections: dict[str, bool] | None = None,
) -> proposal_models.Call:
    """Create a draft copy of ``source`` with the chosen configuration sections.

    ``sections`` is a mapping of `copy_*` flags (see
    ``DUPLICATE_CALL_SECTION_DEFAULTS``); missing keys default to ``True``.
    Proposals, reviews, team permissions, and reviewer-pool memberships are
    never copied regardless of options.
    """
    opts = _resolve_sections(sections)

    kwargs = _clone_concrete_fields(source, _DUPLICATE_CALL_OVERRIDE_FIELDS)
    new_call = proposal_models.Call.objects.create(
        name=new_name,
        state=CallStates.DRAFT,
        created_by=created_by,
        **kwargs,
    )

    if opts["copy_documents"]:
        new_call.documents.set(source.documents.all())

    # RequestedOfferings: copy with state reset; build old→new map so dependent
    # CallResourceTemplate rows can remap their foreign key.
    requested_offering_map: dict[int, proposal_models.RequestedOffering] = {}
    if opts["copy_offerings"]:
        for src_ro in source.requestedoffering_set.all():  # type: ignore
            src_pk = src_ro.pk
            _prepare_clone(src_ro)
            src_ro.call = new_call
            src_ro.state = RequestedOfferingStates.REQUESTED
            src_ro.approved_by = None
            src_ro.save()
            requested_offering_map[src_pk] = src_ro

    if opts["copy_rounds"]:
        for src_round in source.round_set.all():  # type: ignore
            _prepare_clone(src_round)
            src_round.slug = ""
            src_round.call = new_call
            src_round.save()

    if opts["copy_workflow_steps"]:
        # Mandatory steps (e.g. allocation_decision) are pre-seeded on the new
        # call by the post_save signal, so a blind insert would collide on the
        # (call, step) unique constraint. Upsert keyed on (call, step) instead.
        for src_step in source.workflow_steps.all():  # type: ignore
            step_fields = _clone_concrete_fields(
                src_step, _DUPLICATE_WORKFLOW_STEP_OVERRIDE_FIELDS
            )
            proposal_models.CallWorkflowStep.objects.update_or_create(
                call=new_call,
                step=src_step.step,
                defaults=step_fields,
            )

    # Resource templates depend on RequestedOffering FKs — only copy when the
    # parent offerings were copied too.
    if opts["copy_resource_templates"] and opts["copy_offerings"]:
        for src_template in source.resource_templates.all():  # type: ignore
            src_template.requested_offering = requested_offering_map.get(
                src_template.requested_offering_id
            )
            _prepare_clone(src_template)
            src_template.call = new_call
            src_template.save()

    if opts["copy_role_mappings"]:
        for src_mapping in source.proposalprojectrolemapping_set.all():  # type: ignore
            _prepare_clone(src_mapping)
            src_mapping.call = new_call
            src_mapping.save()

    onetoone_targets = (
        ("copy_applicant_visibility_config", "applicant_visibility_config"),
        ("copy_coi_configuration", "coi_configuration"),
        ("copy_matching_configuration", "matching_configuration"),
        ("copy_assignment_configuration", "assignment_configuration"),
    )
    for flag, related_name in onetoone_targets:
        if not opts[flag]:
            continue
        try:
            src_config = getattr(source, related_name)
        except ObjectDoesNotExist:
            continue
        _prepare_clone(src_config)
        src_config.call = new_call
        src_config.save()

    return new_call


def _bulk_round_interval_months(validated_data: dict) -> int:
    cadence = validated_data["cadence"]
    if cadence == BulkRoundCadence.CUSTOM:
        return int(validated_data["custom_interval_months"])
    return BulkRoundCadence.INTERVAL_MONTHS[cadence]


@transaction.atomic
def bulk_create_rounds(
    call: proposal_models.Call, validated_data: dict
) -> list[proposal_models.Round]:
    """Create ``number_of_rounds`` rounds on ``call`` spaced by ``cadence``.

    ``validated_data`` comes from
    :class:`BulkRoundCreateRequestSerializer`. The whole batch is atomic:
    a single overlap with an existing round aborts everything.
    """
    interval_months = _bulk_round_interval_months(validated_data)
    start_time: datetime.datetime = validated_data["start_time"]
    submission_window_days: int = validated_data["submission_window_days"]
    number_of_rounds: int = validated_data["number_of_rounds"]
    window = datetime.timedelta(days=submission_window_days)

    # Per-round fields shared across the whole batch.
    shared_kwargs = {
        k: v
        for k, v in validated_data.items()
        if k
        not in {
            "start_time",
            "cadence",
            "custom_interval_months",
            "submission_window_days",
            "number_of_rounds",
        }
    }
    # Mirror ProtectedRoundSerializer.create()'s fallback.
    shared_kwargs.setdefault("review_duration_in_days", config.PROPOSAL_REVIEW_DURATION)

    created: list[proposal_models.Round] = []
    for i in range(number_of_rounds):
        round_start = start_time + relativedelta(months=interval_months * i)
        round_cutoff = round_start + window

        # Overlap check against pre-existing rounds AND siblings created
        # earlier in this loop (those don't have IDs yet, so compare
        # against the in-memory list too).
        if proposal_models.Round.objects.filter(
            call=call,
            start_time__lt=round_cutoff,
            cutoff_time__gt=round_start,
        ).exists() or any(
            r.start_time < round_cutoff and r.cutoff_time > round_start for r in created
        ):
            raise serializers.ValidationError(
                {
                    "start_time": (
                        f"Round {i + 1} ({round_start.date()} – "
                        f"{round_cutoff.date()}) overlaps with an existing "
                        f"round on this call."
                    )
                }
            )

        round_obj = proposal_models.Round.objects.create(
            call=call,
            start_time=round_start,
            cutoff_time=round_cutoff,
            **shared_kwargs,
        )
        created.append(round_obj)

    return created
