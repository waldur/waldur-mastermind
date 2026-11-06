"""The call workflow, part 2 of 3: existing calls and proposals filled in.

Existing calls keep behaving as they did.

Call steps
----------
1. Every call gets a row for each catalogue step it lacks, disabled, because
   submission gave each proposal an instance -- skipped where the call had no
   enabled row -- for every catalogue step, and each instance now needs a row.
2. Each row takes the kind, name, description, outcome set, completer and
   negative-outcome behaviour of the catalogue step it was.
3. ``order`` is the catalogue index: the engine walked the catalogue and
   ignored ``display_order``, so in-flight proposals keep their path.
4. Each instance points at its call's row of the same key and copies its
   order and allocation decision flag -- the path the proposal walks from now
   on, whatever later happens to the call's configuration. A key the call has
   no row for gets a disabled one. Each proposal points at the instance of the
   step it stands in.
5. A draft or active call left with no enabled step -- one older than the
   rows -- gets the steps a new call starts with, the administrative check and
   the allocation decision, so that what is submitted to it has somewhere to
   go. Proposals already submitted keep their path.
6. A score threshold in a draft or active call that no enabled step before
   it scores -- develop counted the proposal's reviews, however they came to
   be -- is logged and left as it is, for the call manager to settle.

Notification rules already point at the rows and need nothing.

Contributions
-------------
1. Every assessment step says who contributes to it: the step collecting
   reviews (outcome set ``reviewed``) its assigned reviewers, an offering
   manager's step the requested offerings' teams, a step of a call role the
   holders of that role. A step whose gates counted the proposal's reviews
   (``min_reviewers`` / ``min_score_threshold`` on a step that does not
   collect them) keeps counting them, with no contributors.
2. Every review on a proposal that has the call's review step gets a
   contribution there, linked to its review and assignment, with a status
   from the review's state; the review gate then counts the same reviews.
3. Answers to a step's form were kept per proposal and told apart by their
   user, and every step using the form read the same answers. Each answering
   user gets a contribution of their own on every step of the proposal using
   that form that was not skipped, holding their answers unchanged: the first
   such step's completion takes them, the others a copy, so a later step that
   shares the form still finds them. Contributions on a step that is over are
   submitted; on a step still to finish they stay open, so their contributors
   can still edit them.
4. Every assignment batch contributes to its call's review step.

Reviewers
---------
A call's reviewer pool and its reviewer role become one list (see
reviewer_membership): every holder of the role joins the pool as accepted --
an invitation sent to their address is bound to them, not duplicated -- and
every accepted member gets the role. An entry the reviewer declined, or that
expired, keeps that answer and is logged for the call's managers to settle.

Roles and permissions
---------------------
Existing roles get the permissions this release adds, and projects get the
roles of an awarded proposal's team, for stacks that never run import_roles.

Reviewer statistics
-------------------
Only an import wrote them; they are now worked out from the reviews. The rows
the next migration drops with their table are logged.

The schema around this migration: 0093 adds what is filled here, 0095 then
makes it required and unique, and drops what is no longer read.
"""

import logging
from collections import defaultdict

from django.db import migrations
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone
from modeltranslation.settings import DEFAULT_LANGUAGE
from modeltranslation.utils import build_localized_fieldname

logger = logging.getLogger(__name__)

# The catalogue as it stood, frozen here: the live code no longer has one.
# key, kind, name, description, outcome set, completer, on negative,
# allocation decision
CATALOGUE = (
    (
        "administrative_check",
        "decision",
        "Administrative check",
        "Eligibility and completeness validation before evaluation.",
        "eligibility",
        "call_manager",
        "reject",
        False,
    ),
    (
        "technical_assessment",
        "assessment",
        "Technical assessment",
        "Service provider verifies technical feasibility of the proposal.",
        "feasibility",
        "offering_manager",
        "reject",
        False,
    ),
    (
        "expert_review",
        "assessment",
        "Expert review",
        "Independent peer review by domain experts with scoring.",
        "reviewed",
        "reviewer",
        "reject",
        False,
    ),
    (
        "panel_review",
        "decision",
        "Panel review",
        "Collective panel evaluation consolidating expert reviews.",
        "approval",
        "panel_member",
        "reject",
        False,
    ),
    (
        "allocation_decision",
        "decision",
        "Allocation decision",
        "Final decision to approve or reject the proposal and allocate resources.",
        "approval",
        "call_manager",
        "reject",
        True,
    ),
    (
        "award_response",
        "applicant_action",
        "Award response",
        "Applicant explicitly accepts or declines the award before provisioning.",
        "acceptance",
        "applicant",
        "cancel",
        False,
    ),
)

BATCH_SIZE = 1000

INSTANCE_FIELDS = ["call_step", "order", "is_allocation_decision"]

# The steps a new call is created with (handlers.seed_workflow_steps).
CALL_MANAGER_STEPS = ("administrative_check", "allocation_decision")

STEP_FIELDS = [
    "is_enabled",
    "kind",
    "name",
    "description",
    "order",
    "outcome_set",
    "completer",
    "on_negative",
    "is_allocation_decision",
]


def backfill_call_steps(apps, schema_editor):
    Call = apps.get_model("proposal", "Call")
    CallWorkflowStep = apps.get_model("proposal", "CallWorkflowStep")
    Proposal = apps.get_model("proposal", "Proposal")
    ProposalWorkflowStepInstance = apps.get_model(
        "proposal", "ProposalWorkflowStepInstance"
    )

    # 1-3. One row per call and catalogue step, filled from the catalogue.
    open_call_ids = set(
        Call.objects.filter(state__in=("draft", "active")).values_list("id", flat=True)
    )
    for call_id in Call.objects.values_list("id", flat=True).iterator():
        rows = {
            row.key: row for row in CallWorkflowStep.objects.filter(call_id=call_id)
        }
        missing = []
        for order, (
            key,
            kind,
            name,
            description,
            outcome_set,
            completer,
            on_negative,
            is_allocation_decision,
        ) in enumerate(CATALOGUE):
            row = rows.get(key)
            if row is None:
                row = CallWorkflowStep(call_id=call_id, key=key, is_enabled=False)
                missing.append(row)
            row.kind = kind
            row.name = name
            row.description = description
            row.order = order
            row.outcome_set = outcome_set
            row.completer = row.completer or completer
            row.on_negative = on_negative
            row.is_allocation_decision = is_allocation_decision
        # A draft or active call with no enabled step predates the step rows
        # (0056 seeded the decision for draft calls only). New proposals in it
        # would go nowhere, so it gets the call-manager-owned steps a new call
        # is created with (handlers.seed_workflow_steps), as 0064 did for the
        # calls with proposals stranded in them.
        if call_id in open_call_ids and not any(
            row.is_enabled for row in [*rows.values(), *missing]
        ):
            for row in [*rows.values(), *missing]:
                if row.key in CALL_MANAGER_STEPS:
                    row.is_enabled = True
            logger.warning(
                "Call %s had no enabled step; enabled %s.",
                call_id,
                ", ".join(CALL_MANAGER_STEPS),
            )
        # The catalogue was enforced by the API only; a row it never knew
        # goes last, as a decision named after its key.
        catalogue_keys = {entry[0] for entry in CATALOGUE}
        for offset, row in enumerate(
            row for key, row in sorted(rows.items()) if key not in catalogue_keys
        ):
            row.kind = "decision"
            row.name = row.key
            row.order = len(CATALOGUE) + offset
            row.outcome_set = "approval"
            row.completer = row.completer or "call_manager"
        # uuid and the timestamps have Python-side defaults, which bulk_create
        # fills in.
        CallWorkflowStep.objects.bulk_create(missing)
        CallWorkflowStep.objects.bulk_update(
            [row for row in rows.values()], STEP_FIELDS
        )

    # 4a. Every instance points at its call's row of the same key, and keeps
    # a copy of the row's order and allocation decision flag: the path the
    # proposal was submitted with.
    rows = {
        (call_id, key): (pk, order, is_allocation_decision)
        for pk, call_id, key, order, is_allocation_decision in (
            CallWorkflowStep.objects.values_list(
                "pk", "call_id", "key", "order", "is_allocation_decision"
            )
        )
    }
    batch = []
    for instance in (
        ProposalWorkflowStepInstance.objects.filter(call_step__isnull=True)
        .select_related("proposal__round")
        .only("pk", "step", "proposal__round__call_id")
        .iterator(chunk_size=BATCH_SIZE)
    ):
        call_id = instance.proposal.round.call_id
        if (call_id, instance.step) not in rows:
            rows[(call_id, instance.step)] = _add_unknown_row(
                CallWorkflowStep, call_id, instance.step
            )
            logger.warning(
                "Proposal %s has a %s step its call had no row for; added it disabled.",
                instance.proposal_id,
                instance.step,
            )
        (
            instance.call_step_id,
            instance.order,
            instance.is_allocation_decision,
        ) = rows[(call_id, instance.step)]
        batch.append(instance)
        if len(batch) >= BATCH_SIZE:
            ProposalWorkflowStepInstance.objects.bulk_update(batch, INSTANCE_FIELDS)
            batch = []
    ProposalWorkflowStepInstance.objects.bulk_update(batch, INSTANCE_FIELDS)

    # 4b. Every proposal in the workflow points at the instance of its step.
    Proposal.objects.filter(workflow_step__isnull=False).update(
        current_step_id=Subquery(
            ProposalWorkflowStepInstance.objects.filter(
                proposal_id=OuterRef("pk"), step=OuterRef("workflow_step")
            )
            .order_by("-created")
            .values("pk")[:1]
        )
    )
    for pk, step in Proposal.objects.filter(
        workflow_step__isnull=False, current_step__isnull=True
    ).values_list("pk", "workflow_step"):
        logger.warning(
            "Proposal %s stood on the %s step but has no instance of it; it is "
            "left outside the workflow.",
            pk,
            step,
        )


def _add_unknown_row(CallWorkflowStep, call_id, key):
    """A disabled row, last in its call, for a key no row of the call has."""
    last = (
        CallWorkflowStep.objects.filter(call_id=call_id)
        .order_by("-order")
        .values_list("order", flat=True)
        .first()
    )
    row = CallWorkflowStep.objects.create(
        call_id=call_id,
        key=key,
        kind="decision",
        name=key,
        order=0 if last is None else last + 1,
        outcome_set="approval",
        completer="call_manager",
        is_enabled=False,
    )
    return row.pk, row.order, False


def restore_workflow_step(apps, schema_editor):
    """Going back: the proposal's step key comes from the instance it points at."""
    Proposal = apps.get_model("proposal", "Proposal")
    batch = []
    for proposal in (
        Proposal.objects.filter(current_step__isnull=False)
        .select_related("current_step")
        .iterator(chunk_size=BATCH_SIZE)
    ):
        proposal.workflow_step = proposal.current_step.step
        batch.append(proposal)
    Proposal.objects.bulk_update(batch, ["workflow_step"], batch_size=BATCH_SIZE)


CALL_ROLES = ("call_manager", "reviewer", "panel_member")
REVIEW_STATUSES = {
    "in_review": "accepted",
    "submitted": "submitted",
    "rejected": "declined",
}
FINISHED_STEP_STATUSES = ("completed", "expired")


def _contributor_source(step):
    if step.kind != "assessment":
        return "none"
    if step.outcome_set == "reviewed":
        return "assigned_reviewers"
    if step.min_contributions is not None or step.min_score_threshold is not None:
        return "none"
    if step.completer == "offering_manager":
        return "requested_offering_team"
    if step.completer in CALL_ROLES:
        return f"call_role:{step.completer}"
    return "none"


def _log_score_gates_without_scores(CallWorkflowStep):
    """Name the open calls whose score threshold nothing before it scores.

    Develop counted every submitted review of the proposal, however it came
    to be; the workflow check now refuses to activate such a call. They are
    left as they are -- proposals already reviewed still pass -- for their
    call manager to enable a review step or drop the threshold.
    """
    enabled = CallWorkflowStep.objects.filter(
        is_enabled=True, call__state__in=("draft", "active")
    )
    for step in enabled.filter(
        min_score_threshold__isnull=False, contributor_source="none"
    ).order_by("call_id", "order"):
        if (
            enabled.filter(call_id=step.call_id, order__lt=step.order)
            .exclude(contributor_source="none")
            .exists()
        ):
            continue
        logger.warning(
            "Call %s: the %s step needs a score of %s, and no enabled step "
            "before it collects reviews. The workflow check reports it as an "
            "error until a review step is enabled or the threshold removed.",
            step.call_id,
            step.key,
            step.min_score_threshold,
        )


def _review_steps(CallWorkflowStep):
    """call id -> the call's step collecting reviews, as review_step_for picks it."""
    review_steps = {}
    for step in CallWorkflowStep.objects.filter(
        contributor_source="assigned_reviewers"
    ).order_by("call_id", "-is_enabled", "order", "id"):
        review_steps.setdefault(step.call_id, step)
    return review_steps


def _set_contributor_sources(CallWorkflowStep):
    for step in CallWorkflowStep.objects.filter(kind="assessment"):
        source = _contributor_source(step)
        if source != step.contributor_source:
            step.contributor_source = source
            step.save(update_fields=["contributor_source"])


def _backfill_reviews(apps, review_steps):
    """One contribution per review, on the step its assignment was made for.

    A review whose assignment batch names a step of the call contributes to
    that step; any other to the call's first step collecting reviews.
    """
    Review = apps.get_model("proposal", "Review")
    AssignmentItem = apps.get_model("proposal", "AssignmentItem")
    Instance = apps.get_model("proposal", "ProposalWorkflowStepInstance")
    StepContribution = apps.get_model("proposal", "StepContribution")

    items = {
        item.review_id: item
        for item in AssignmentItem.objects.filter(review__isnull=False).select_related(
            "batch"
        )
    }
    instances = {}
    for instance in Instance.objects.exclude(status="skipped").order_by("id"):
        instances.setdefault((instance.proposal_id, instance.call_step_id), instance)
    contributions = []
    for review in Review.objects.select_related("proposal__round").order_by("id"):
        item = items.get(review.id)
        instance = None
        if item is not None and item.batch.call_step_id:
            instance = instances.get((review.proposal_id, item.batch.call_step_id))
        if instance is None:
            call_step = review_steps.get(review.proposal.round.call_id)
            if call_step is not None:
                instance = instances.get((review.proposal_id, call_step.id))
        if instance is None:
            continue
        status = REVIEW_STATUSES.get(review.state, "accepted")
        contributions.append(
            StepContribution(
                proposal_step=instance,
                contributor_id=review.reviewer_id,
                review=review,
                assignment_item=item,
                status=status,
                submitted_at=review.modified if status == "submitted" else None,
                due_at=instance.deadline,
            )
        )
    StepContribution.objects.bulk_create(contributions, batch_size=500)
    return len(contributions)


def _update_completion_status(completion, Question, Answer):
    answers = Answer.objects.filter(completion_id=completion.id)
    answered = set(answers.values_list("question_id", flat=True))
    required = set(
        Question.objects.filter(
            checklist_id=completion.checklist_id, required=True
        ).values_list("id", flat=True)
    )
    completion.is_completed = required <= answered
    completion.requires_review = answers.filter(requires_review=True).exists()
    completion.save(update_fields=["is_completed", "requires_review"])


def _contribution_completion(
    instance, user_id, last, checklist_id, StepContribution, Completion, contribution_ct
):
    """``user_id``'s completion of the form in their contribution to
    ``instance``, both made if missing."""
    finished = instance.status in FINISHED_STEP_STATUSES
    contribution = (
        StepContribution.objects.filter(
            proposal_step=instance,
            contributor_id=user_id,
            requested_offering__isnull=True,
        )
        .order_by("review_id", "id")
        .first()
    )
    if contribution is None:
        contribution = StepContribution.objects.create(
            proposal_step=instance,
            contributor_id=user_id,
            status="submitted" if finished else "pending",
            submitted_at=last if finished else None,
            due_at=instance.deadline,
        )
    target, _ = Completion.objects.get_or_create(
        scope_content_type=contribution_ct,
        scope_object_id=contribution.id,
        checklist_id=checklist_id,
    )
    return target


ANSWER_COPY_FIELDS = (
    "question_id",
    "answer_data",
    "requires_review",
    "reviewed_by_id",
    "reviewed_at",
    "review_notes",
)


def _copy_answers(Answer, source_id, target_id, user_id):
    """Copy ``user_id``'s answers from one completion to another, verbatim:
    no save() of the live model runs here to reprocess them."""
    have = set(
        Answer.objects.filter(completion_id=target_id, user_id=user_id).values_list(
            "question_id", flat=True
        )
    )
    Answer.objects.bulk_create(
        Answer(
            completion_id=target_id,
            user_id=user_id,
            **{field: getattr(answer, field) for field in ANSWER_COPY_FIELDS},
        )
        for answer in Answer.objects.filter(completion_id=source_id, user_id=user_id)
        if answer.question_id not in have
    )


def _backfill_answers(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    CallWorkflowStep = apps.get_model("proposal", "CallWorkflowStep")
    Proposal = apps.get_model("proposal", "Proposal")
    Instance = apps.get_model("proposal", "ProposalWorkflowStepInstance")
    StepContribution = apps.get_model("proposal", "StepContribution")
    Completion = apps.get_model("checklist", "ChecklistCompletion")
    Answer = apps.get_model("checklist", "Answer")
    Question = apps.get_model("checklist", "Question")

    proposal_ct = ContentType.objects.get_for_model(Proposal)
    contribution_ct = ContentType.objects.get_for_model(StepContribution)

    # (call id, checklist id) -> the call's steps using that form, in order
    steps_by_form = defaultdict(list)
    for step in CallWorkflowStep.objects.filter(checklist__isnull=False).order_by(
        "call_id", "order", "id"
    ):
        steps_by_form[(step.call_id, step.checklist_id)].append(step)
    if not steps_by_form:
        return 0

    moved = 0
    for completion in Completion.objects.filter(
        scope_content_type=proposal_ct,
        checklist_id__in={checklist_id for _, checklist_id in steps_by_form},
    ).order_by("id"):
        proposal = (
            Proposal.objects.select_related("round__call")
            .filter(pk=completion.scope_object_id)
            .first()
        )
        if proposal is None:
            continue
        call = proposal.round.call
        # The applicant's compliance answers stay where they are.
        if call.compliance_checklist_id == completion.checklist_id:
            continue
        steps = steps_by_form.get((call.id, completion.checklist_id))
        if not steps:
            continue
        instances = list(
            Instance.objects.filter(proposal=proposal, call_step__in=steps)
            .exclude(status="skipped")
            .order_by("order", "id")
        ) or list(
            Instance.objects.filter(proposal=proposal, call_step__in=steps).order_by(
                "order", "id"
            )
        )
        if not instances:
            logger.warning(
                "Answers of proposal %s to checklist %s belong to no step of it; "
                "left in place.",
                proposal.pk,
                completion.checklist_id,
            )
            continue
        # Every step using the form read these answers; one that never ran
        # gets them only when no step using the form ran at all.
        if instances[0].status == "skipped":
            instances = instances[:1]

        users = (
            Answer.objects.filter(completion_id=completion.id)
            .values("user_id")
            .annotate(last=Max("modified"))
            .order_by("user_id")
        )
        for row in users:
            user_id = row["user_id"]
            first_target = None
            for instance in instances:
                target = _contribution_completion(
                    instance,
                    user_id,
                    row["last"],
                    completion.checklist_id,
                    StepContribution,
                    Completion,
                    contribution_ct,
                )
                if first_target is None:
                    first_target = target
                    moved += Answer.objects.filter(
                        completion_id=completion.id, user_id=user_id
                    ).update(completion_id=target.id)
                else:
                    _copy_answers(Answer, first_target.id, target.id, user_id)
                _update_completion_status(target, Question, Answer)

        if not Answer.objects.filter(completion_id=completion.id).exists():
            completion.delete()
    return moved


def backfill_step_contributions(apps, schema_editor):
    CallWorkflowStep = apps.get_model("proposal", "CallWorkflowStep")
    AssignmentBatch = apps.get_model("proposal", "AssignmentBatch")

    _set_contributor_sources(CallWorkflowStep)
    _log_score_gates_without_scores(CallWorkflowStep)
    review_steps = _review_steps(CallWorkflowStep)
    reviews = _backfill_reviews(apps, review_steps)
    answers = _backfill_answers(apps, schema_editor)
    for call_id, step in review_steps.items():
        AssignmentBatch.objects.filter(call_id=call_id, call_step__isnull=True).update(
            call_step=step
        )
    logger.info(
        "Backfilled %s review contributions and moved %s answers into contributions.",
        reviews,
        answers,
    )


def restore_proposal_answers(apps, schema_editor):
    """Move the answers back to their proposal's completion of the form.

    Where one user answered one question in two steps sharing the form, the
    first step's answer is kept.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    Proposal = apps.get_model("proposal", "Proposal")
    StepContribution = apps.get_model("proposal", "StepContribution")
    Completion = apps.get_model("checklist", "ChecklistCompletion")
    Answer = apps.get_model("checklist", "Answer")
    Question = apps.get_model("checklist", "Question")

    proposal_ct = ContentType.objects.get_for_model(Proposal)
    contribution_ct = ContentType.objects.get_for_model(StepContribution)
    for completion in Completion.objects.filter(
        scope_content_type=contribution_ct
    ).order_by("id"):
        contribution = (
            StepContribution.objects.select_related("proposal_step")
            .filter(pk=completion.scope_object_id)
            .first()
        )
        if contribution is not None:
            target, _ = Completion.objects.get_or_create(
                scope_content_type=proposal_ct,
                scope_object_id=contribution.proposal_step.proposal_id,
                checklist_id=completion.checklist_id,
            )
            for answer in Answer.objects.filter(completion_id=completion.id):
                if Answer.objects.filter(
                    completion_id=target.id,
                    question_id=answer.question_id,
                    user_id=answer.user_id,
                ).exists():
                    answer.delete()
                else:
                    answer.completion_id = target.id
                    answer.save(update_fields=["completion"])
            _update_completion_status(target, Question, Answer)
        # Its answers have all moved or gone, and nothing else points at a
        # completion. Deleting without the collector: unapplying this
        # migration's two operations renders the checklist models twice, and
        # the collector refuses to match the two classes.
        Completion.objects.filter(pk=completion.pk)._raw_delete(
            schema_editor.connection.alias
        )


# Mirrors docker/rootfs/etc/waldur/permissions.yaml, for stacks that never run
# import_roles and for organization clones, which copied their template's
# permissions once and never see later additions.
NEW_GRANTS = {
    ("customer", "CUSTOMER.OWNER"): ["PROJECT.SET_END_DATE", "RESOURCE.PULL"],
    ("serviceprovider", "CUSTOMER.MANAGER"): ["RESOURCE.PULL"],
    ("offering", "OFFERING.MANAGER"): ["RESOURCE.PULL"],
    ("project", "PROJECT.ADMIN"): [
        "PROJECT.REQUEST_END_DATE_CHANGE",
        "RESOURCE.REQUEST_END_DATE_CHANGE",
        "RESOURCE.REQUEST_LIMITS_CHANGE",
        "RESOURCE.PULL",
    ],
    ("project", "PROJECT.MANAGER"): [
        "PROJECT.REQUEST_END_DATE_CHANGE",
        "RESOURCE.REQUEST_END_DATE_CHANGE",
        "RESOURCE.REQUEST_LIMITS_CHANGE",
        "RESOURCE.PULL",
    ],
    ("project", "PROJECT.MEMBER"): [
        "PROJECT.REQUEST_END_DATE_CHANGE",
        "RESOURCE.REQUEST_END_DATE_CHANGE",
        "RESOURCE.REQUEST_LIMITS_CHANGE",
    ],
    ("customer", "CUSTOMER.READER"): ["CALL.VIEW_RESULTS"],
}

AWARDED_PROJECT_ROLES = (
    (
        "PROJECT.PRINCIPAL_INVESTIGATOR",
        "Principal investigator",
        "PROJECT.MANAGER",
        [
            "PROJECT.LIST",
            "PROJECT.VIEW_TEAM",
            "PROJECT.CREATE_PERMISSION",
            "PROJECT.UPDATE_PERMISSION",
            "PROJECT.DELETE_PERMISSION",
            "PROJECT.REVIEW_MEMBERSHIP",
            "INVITATION.LIST",
            "ORDER.LIST",
            "RESOURCE.LIST",
            "RESOURCE.PULL",
            "RESOURCE.CREATE_PERMISSION",
            "RESOURCE.UPDATE_PERMISSION",
            "RESOURCE.DELETE_PERMISSION",
            "RESOURCE_PROJECT.CREATE_PERMISSION",
            "RESOURCE_PROJECT.UPDATE_PERMISSION",
            "RESOURCE_PROJECT.DELETE_PERMISSION",
        ],
    ),
    (
        "PROJECT.CO_INVESTIGATOR",
        "Co-investigator",
        "PROJECT.ADMIN",
        [
            "PROJECT.LIST",
            "PROJECT.VIEW_TEAM",
            "ORDER.LIST",
            "RESOURCE.LIST",
            "RESOURCE.PULL",
        ],
    ),
    (
        "PROJECT.COLLABORATOR",
        "Collaborator",
        "PROJECT.MEMBER",
        ["PROJECT.LIST", "PROJECT.VIEW_TEAM", "RESOURCE.LIST"],
    ),
)


def grant_new_permissions(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    for (model, name), permissions in NEW_GRANTS.items():
        roles = Role.objects.filter(
            content_type__model=model, is_system_role=True, name=name
        )
        clones = Role.objects.filter(
            content_type__model=model,
            template__name=name,
            template__is_system_role=True,
        )
        for role in roles.union(clones):
            for permission in permissions:
                RolePermission.objects.get_or_create(role=role, permission=permission)


def create_awarded_project_roles(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    project_type, _ = ContentType.objects.get_or_create(
        app_label="structure", model="project"
    )
    for name, description, template_name, permissions in AWARDED_PROJECT_ROLES:
        template = Role.objects.filter(
            name=template_name, content_type=project_type
        ).first()
        # Role.description is a modeltranslation field and a historical model
        # has no translation descriptors, so the served column is written
        # explicitly.
        role, created = Role.objects.get_or_create(
            name=name,
            content_type=project_type,
            defaults={
                "description": description,
                build_localized_fieldname("description", DEFAULT_LANGUAGE): (
                    description
                ),
                "is_system_role": True,
                "template": template,
            },
        )
        if created:
            for permission in permissions:
                RolePermission.objects.create(role=role, permission=permission)


def sync_call_reviewers(apps, schema_editor):
    """One list of a call's reviewers (see reviewer_membership): put every
    holder of a call's reviewer role in its pool as accepted, and give every
    accepted pool member the role.

    A holder's entry may be an invitation sent to their address; it is bound
    to them, not duplicated. An entry the reviewer declined, or that expired,
    keeps that answer: it is logged for the call's managers to settle.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    Role = apps.get_model("permissions", "Role")
    UserRole = apps.get_model("permissions", "UserRole")
    CallReviewerPool = apps.get_model("proposal", "CallReviewerPool")
    ReviewerProfile = apps.get_model("proposal", "ReviewerProfile")
    call_type = ContentType.objects.filter(app_label="proposal", model="call").first()
    role = Role.objects.filter(name="CALL.REVIEWER").first()
    if call_type is None or role is None:
        return
    roles = Role.objects.filter(
        Q(pk=role.pk) | Q(template=role), content_type=call_type
    )
    now = timezone.now()
    for grant in UserRole.objects.filter(
        content_type=call_type, role__in=roles, is_active=True
    ).select_related("user"):
        user = grant.user
        pool = CallReviewerPool.objects.filter(call_id=grant.object_id).order_by("pk")
        entry = pool.filter(Q(reviewer__user=user) | Q(invited_user=user)).first()
        if entry is None and user.email:
            # An invitation sent to their address that no account has taken
            # up: the pool holds one entry per address, so it is theirs.
            entry = pool.filter(
                reviewer__isnull=True,
                invited_user__isnull=True,
                invited_email__iexact=user.email,
            ).first()
            if entry is not None:
                profile = ReviewerProfile.objects.filter(user=user).first()
                if profile is not None and not pool.filter(reviewer=profile).exists():
                    entry.reviewer = profile
                entry.invited_user = user
                entry.save(update_fields=["reviewer", "invited_user"])
        if entry is not None:
            if entry.invitation_status in ("declined", "expired"):
                # The reviewer's own answer is kept: the role was given
                # without asking them, and a call manager settles it.
                logger.warning(
                    "%s holds the reviewer role of call %s but its pool "
                    "invitation is %s; both are left as they are.",
                    user.username,
                    grant.object_id,
                    entry.invitation_status,
                )
            elif entry.invitation_status != "accepted":
                entry.invitation_status = "accepted"
                entry.response_date = now
                entry.override_reason = "Given the call's reviewer role."
                entry.save(
                    update_fields=[
                        "invitation_status",
                        "response_date",
                        "override_reason",
                    ]
                )
            continue
        profile = ReviewerProfile.objects.filter(user=user).first()
        if profile is None and not user.email:
            continue
        CallReviewerPool.objects.create(
            call_id=grant.object_id,
            reviewer=profile,
            invited_user=None if profile else user,
            invited_email="" if profile else user.email,
            invitation_status="accepted",
            response_date=now,
            override_reason="Given the call's reviewer role.",
        )
    for entry in CallReviewerPool.objects.filter(
        invitation_status="accepted"
    ).select_related("reviewer"):
        user_id = entry.reviewer.user_id if entry.reviewer_id else entry.invited_user_id
        if user_id is None:
            continue
        if UserRole.objects.filter(
            content_type=call_type,
            object_id=entry.call_id,
            user_id=user_id,
            role__in=roles,
            is_active=True,
        ).exists():
            continue
        UserRole.objects.create(
            content_type=call_type,
            object_id=entry.call_id,
            user_id=user_id,
            role=role,
            is_active=True,
        )


def log_dropped_reviewer_stats(apps, schema_editor):
    """Say what is lost with the table. Only an import wrote to it; the
    figures are now worked out from the reviews, so what goes is history
    from outside this database."""
    ReviewerStats = apps.get_model("proposal", "ReviewerStats")
    count = ReviewerStats.objects.count()
    if count:
        logger.warning(
            "Dropping %s imported reviewer stats rows (reviewer profiles %s). "
            "Reviewer figures are now worked out from this database's reviews; "
            "the imported ones are not kept.",
            count,
            ", ".join(
                str(pk)
                for pk in ReviewerStats.objects.order_by(
                    "reviewer_profile_id"
                ).values_list("reviewer_profile_id", flat=True)
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0093_call_workflow_expand"),
        ("checklist", "0017_checklist_organization"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("permissions", "0038_invoice_list_permission"),
    ]

    operations = [
        migrations.RunPython(backfill_call_steps, restore_workflow_step),
        migrations.RunPython(backfill_step_contributions, restore_proposal_answers),
        migrations.RunPython(grant_new_permissions, migrations.RunPython.noop),
        migrations.RunPython(create_awarded_project_roles, migrations.RunPython.noop),
        migrations.RunPython(sync_call_reviewers, migrations.RunPython.noop),
        migrations.RunPython(log_dropped_reviewer_stats, migrations.RunPython.noop),
    ]
