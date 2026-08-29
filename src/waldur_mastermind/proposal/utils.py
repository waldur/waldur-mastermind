import datetime
import logging
import uuid
from typing import cast

from constance import config
from dateutil.relativedelta import relativedelta
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from waldur_core.core import utils as core_utils
from waldur_core.core.fields import StringUUID
from waldur_core.core.utils import get_system_robot
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import order_approval
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import (
    AllocationTimes,
    BulkRoundCadence,
    CallStates,
    RequestedOfferingStates,
)

logger = logging.getLogger(__name__)


def _requested_months(
    requested_resource: proposal_models.RequestedResource,
) -> int | None:
    """How many whole months the request asks for, or None when it names none.

    The length is the contract. ``attributes.end_date`` is the older form of the
    same answer, kept for requests written before the form asked for months, and
    measured from the day that request was created because that is the day its
    date was computed from.
    """
    attributes = requested_resource.attributes or {}

    stored_length = attributes.get("prepaid_duration_months")
    if stored_length is not None:
        try:
            months = int(stored_length)
        except (TypeError, ValueError):
            months = 0
        if months > 0:
            return months
        logger.warning(
            "Requested resource %s carries an unusable subscription length %r; "
            "falling back to its end date.",
            requested_resource.uuid,
            stored_length,
        )

    if not attributes.get("end_date"):
        return None

    try:
        requested_end = marketplace_utils.parse_date(attributes["end_date"])
    except serializers.ValidationError:
        logger.warning(
            "Requested resource %s carries an unparseable end date %r; the "
            "allocated resource is left without one.",
            requested_resource.uuid,
            attributes["end_date"],
        )
        return None
    if requested_end is None:
        return None

    return core_utils.calculate_duration_months(
        requested_resource.created.date(), requested_end
    )


def _is_prepaid(requested_resource: proposal_models.RequestedResource) -> bool:
    """Whether this request buys a subscription at all.

    The stored length means nothing on an offering with no prepaid component —
    only such an offering is bought by the month.
    """
    return requested_resource.requested_offering.offering.components.filter(
        is_prepaid=True
    ).exists()


def get_proposal_duration_months(proposal: proposal_models.Proposal) -> int | None:
    """The longest subscription the proposal asks for, in whole months.

    The project cannot end before its longest subscription does. Returns None
    when the proposal asks for no subscription at all — a call may accept
    prepaid and non-prepaid offerings side by side, and a proposal that requested
    only the latter has no length to derive anything from.
    """
    lengths = [
        months
        for requested_resource in proposal.requestedresource_set.filter(
            requested_offering__state=RequestedOfferingStates.ACCEPTED
        ).select_related("requested_offering__offering")
        if _is_prepaid(requested_resource)
        and (months := _requested_months(requested_resource)) is not None
    ]
    return max(lengths) if lengths else None


def project_end_date(
    proposal: proposal_models.Proposal, start_date: datetime.date
) -> datetime.date | None:
    """When the allocated project should end, measured from its own start.

    The subscription the applicant asked for wins where there is one; the call's
    fixed duration applies to proposals that asked for none. The two are never
    converted into each other — a length in months and a length in days are only
    comparable once each has been resolved against a date, because a day count is
    true only relative to the anchor it was measured from.
    """
    months = get_proposal_duration_months(proposal)
    if months is not None:
        return start_date + relativedelta(months=months)

    fixed_days = proposal.round.call.fixed_duration_in_days
    if fixed_days:
        return start_date + datetime.timedelta(days=fixed_days)

    return None


def requested_duration_label(proposal: proposal_models.Proposal) -> str | None:
    """The project length as the applicant can be told it, unit included.

    In order of truthfulness: what was granted (once allocated), the
    subscription the proposal asks for, and the call's fixed length. Months
    and days are never converted into each other (see :func:`project_end_date`),
    so the unit travels with the number. None when nothing is known — the
    template drops the line rather than printing "None".
    """
    granted_days = granted_duration_in_days(proposal)
    if granted_days:
        return f"{granted_days} days"

    months = get_proposal_duration_months(proposal)
    if months:
        return "1 month" if months == 1 else f"{months} months"

    fixed_days = proposal.round.call.fixed_duration_in_days
    if fixed_days:
        return f"{fixed_days} days"

    return None


def granted_duration_in_days(
    proposal: proposal_models.Proposal,
) -> int | None:
    """How long the granted project runs, in whole days.

    The counterpart of :func:`project_end_date`: that decides when the project
    ends, this reads the decision back so the applicant can be told.

    Deliberately what was *granted*, not what was asked for: the applicant is
    not asked for a length at all any more (the retired
    ``Proposal.duration_in_days`` recorded one), and the request's own length —
    a subscription in months — is resolved against a date at allocation.

    Returns None when there is nothing truthful to say — no project yet, or a
    project with no end date, which is a grant that does not expire. The
    template drops the line rather than printing a blank.
    """
    project = proposal.project
    if project is None or project.end_date is None:
        return None

    # The anchor allocation itself measured from: the project's own start where
    # it has one, otherwise the day it was created (allocate_proposal passes
    # `start_date or today` to project_end_date and stores start_date as-is).
    start_date = project.start_date or timezone.localdate(project.created)
    days = (project.end_date - start_date).days
    return days if days > 0 else None


def _requested_end_date(
    requested_resource: proposal_models.RequestedResource,
    project: structure_models.Project,
    today: datetime.date,
) -> datetime.date | None:
    """The end date for the allocated resource, anchored on its project's start.

    A resource request names a length, not a date: the day the resource is
    granted is unknown while the proposal is being written and reviewed. Running
    the grant for that many months from the day allocation is scheduled for
    keeps both the period the applicant chose and the cost the reviewer priced,
    where an absolute date would quietly deliver a shorter grant and invoice less
    than the approved figure — and, once review outlasts the period, would have
    passed altogether, which ``validate_end_date`` rejects outright.

    Returns None when no period was requested, or when the anchored date breaks
    the offering's own termination rules — allocation must not fail over a date,
    so the resource is left open and the operator gets a warning.
    """
    months = _requested_months(requested_resource)
    if months is None:
        return None

    # Measured from the day allocation is scheduled for, not from the day the
    # decision happened to be taken. A call that dates allocation forward would
    # otherwise spend the whole interval before the project even opens: a grant
    # approved in August and allocated in December expired in the following
    # August rather than the following December. What happens after that date —
    # the provider approving the order, the backend taking its time — eats into
    # the usable period without moving it.
    anchor = project.start_date or today
    end_date = anchor + relativedelta(months=months)

    # Clamped, not left to be rejected. ``validate_end_date`` raises when a
    # resource outlasts its project, and the handler below turns any rejection
    # into "no end date at all" — which bills a prepaid resource for a single
    # month. A resource that would outrun its project should be shortened to it,
    # not silently un-dated.
    if project.end_date and end_date > project.end_date:
        logger.info(
            "End date %s for requested resource %s is capped at the project's "
            "own end date %s.",
            end_date,
            requested_resource.uuid,
            project.end_date,
        )
        end_date = project.end_date

    offering = requested_resource.requested_offering.offering
    try:
        return marketplace_utils.validate_end_date(
            offering,
            today,
            end_date,
            # The offering's own termination offset is measured from the same
            # anchor the period is, or a date only N months from the project's
            # start reads as N months plus the wait before it. The marketplace's
            # own path says the same thing in validate_end_date_for_resource.
            start_date=project.start_date,
            project_end_date=project.end_date,
        )
    except serializers.ValidationError as exc:
        logger.warning(
            "End date %s for requested resource %s was rejected by offering %s "
            "(%s); the allocated resource is left without one, so prepaid "
            "components are charged for a single month.",
            end_date,
            requested_resource.uuid,
            offering.uuid,
            exc.detail,
        )
        return None


def _purchase_order_requirement_met(order: marketplace_models.Order) -> bool:
    """Whether the offering's own purchase order requirement is satisfied.

    Mirrors the marketplace gate (``order_should_not_be_reviewed_by_consumer``
    and ``approve_by_consumer``), which accepts the document only — a bare
    reference satisfies the proposal but not the provider.
    """
    if not order.offering.plugin_options.get("require_purchase_order_upload", False):
        return True
    return bool(order.attachment)


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

    # The project runs for as long as what it holds: the longest subscription
    # requested, or the call's fixed duration for a proposal that requested no
    # subscription. Measured from the project's own start so that a call which
    # dates allocation forward does not spend the period before it opens.
    # One reading of the clock for the whole allocation: the project and every
    # resource in it must be measured from the same day, or a run that crosses
    # midnight leaves a resource outlasting its own project.
    today = datetime.date.today()
    end_date = project_end_date(proposal, start_date or today)

    project = structure_models.Project.objects.create(
        customer=proposal_round.call.manager.customer,
        name=project_name,
        start_date=start_date,
        end_date=end_date,
    )
    project = cast(structure_models.Project, project)

    if start_date:
        logger.info(
            f"Field start_date of {project} has been changed to {proposal.round.allocation_date}."
        )
    if end_date:
        logger.info(
            "Project %s ends on %s, derived from %s.",
            project,
            end_date,
            "the longest requested subscription"
            if get_proposal_duration_months(proposal) is not None
            else "the call's fixed duration",
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
                project.add_user_or_skip(user, mapping.project_role)
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
            # Before init_cost: the prepaid multiplier in Plan.get_estimate and
            # in the invoice item builder both read this field, so setting it
            # afterwards would price and bill a six-month grant as one month.
            # The marketplace order path sets it in the same order and says so.
            resource.end_date = _requested_end_date(requested_resource, project, today)
            resource.init_cost()
            resource.save()

            robot = get_system_robot()
            order = marketplace_models.Order(
                **attrs,
                resource=resource,
                created_by=robot,
            )
            # Hand the purchase order to the order, so the approval gate in
            # marketplace.permissions is already satisfied. Without this the
            # applicant supplies it during the proposal and is asked again the
            # moment the allocation lands.
            if requested_resource.attachment:
                # Point at the stored file rather than assigning the FieldFile:
                # the document is already committed, so this records the same
                # path without re-uploading a copy.
                order.attachment.name = requested_resource.attachment.name
            if requested_resource.purchase_order_reference:
                order.request_comment = requested_resource.purchase_order_reference
            order.init_cost()
            order.save()

            requested_resource.resource = resource
            requested_resource.save()

            # The call review already authorised this spend, so the consumer
            # approval step has nothing left to decide. Left in
            # PENDING_CONSUMER the order parks the allocated resource in
            # CREATING until somebody clicks approve, and — when the offering
            # demands a purchase order — asks the applicant for the document
            # the proposal already collected, because the marketplace gate
            # looks at order.attachment while the proposal accepts a bare
            # reference as well.
            #
            # Only the consumer step is skipped: the transition below still
            # routes to provider review, and to PENDING_PROJECT for a
            # future-dated project. PENDING_START_DATE is not reachable from
            # here — it needs an order start date, and these orders are built
            # without one. No select_for_update is taken because the row was
            # created in this transaction and is not yet visible to anyone else.
            #
            # The provider's own requirement is not skipped. Its flag lives on
            # the offering and gates order approval; the call setting only
            # decides what the proposal collects, and the two can diverge — the
            # call entry snapshots the flag when the offering is added, so an
            # offering that starts requiring a purchase order later leaves
            # existing calls collecting nothing. Auto-approving there would
            # walk the order straight past a control the provider still holds,
            # so leave it pending and let the usual gate ask for the document.
            if not _purchase_order_requirement_met(order):
                logger.info(
                    "Order %s allocated from proposal %s awaits a purchase "
                    "order, so consumer approval is left to a human.",
                    order.uuid,
                    proposal.uuid,
                )
                continue

            order.review_by_consumer(robot)
            outcome = order_approval.transition_order_from_consumer_approval(
                order, robot
            )
            logger.info(
                "Order %s allocated from proposal %s moved to %s.",
                order.uuid,
                proposal.uuid,
                outcome,
            )


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
    "copy_proposal_field_config": True,
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

    # Not part of the loop below: the new call already has a field config, seeded
    # by the post_save handler, so cloning the source row would collide on the
    # one-to-one constraint. Overwrite the seeded columns instead.
    if opts["copy_proposal_field_config"]:
        source_states = proposal_models.CallProposalFieldConfig.get_states_for_call(
            source
        )
        proposal_models.CallProposalFieldConfig.objects.update_or_create(
            call=new_call,
            defaults={
                proposal_models.CallProposalFieldConfig.column_for(field_name): state
                for field_name, state in source_states.items()
            },
        )

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
