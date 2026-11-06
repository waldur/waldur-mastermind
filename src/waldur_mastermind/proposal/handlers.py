"""Signal handlers for proposal application."""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework.exceptions import ValidationError

from waldur_core.checklist import models as checklist_models
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import RoleChange, RoleEventDetails
from waldur_mastermind.proposal import (
    activity,
    change_requests,
    contributions,
    dry_run_context,
    enums,
    models,
    notification_rules,
    panels,
    proposal_roles,
    reviewer_standing,
    tasks,
    workflow_service,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import RequestedOfferingStates

logger = logging.getLogger(__name__)

TEAM_FROZEN_MESSAGE = (
    "The proposal team cannot be changed once the proposal is submitted. "
    "Ask a call manager to change it."
)
ONLY_MANAGERS_MESSAGE = (
    "Only a proposal manager or a call manager may grant or revoke the "
    "proposal manager and proposal administrator roles."
)
AWARDED_TEAM_FROZEN_MESSAGE = (
    "The proposal team cannot be changed once the award is accepted. "
    "Manage the people on the awarded project instead."
)
EVALUATOR_JOINS_MESSAGE = (
    "This person evaluates the call's proposals. Ask a call manager to add "
    "them to the team of a submitted proposal."
)
OWN_ROLE_MESSAGE = "Nobody can change their own role on a proposal."
LAST_MANAGER_MESSAGE = (
    "A proposal must keep at least one proposal manager. Grant the role to "
    "someone else first."
)


# Roles that only the proposal's managers, those overseeing its call and staff
# may grant or revoke: the manager role itself and the administrator role,
# which edits the proposal.
TEAM_ROLES_MANAGED_BY_MANAGERS = frozenset(
    {RoleEnum.PROPOSAL_MANAGER, RoleEnum.PROPOSAL_ADMIN}
)


def guard_call_reviewer_grant(scope, role, acting_user, change, user=None):
    """Granting the call's reviewer role takes on review work: refused to a
    reviewer the operator has suspended or blocked, or outside the call's
    reviewer eligibility. Revoking it is always possible, and an invitation
    is checked when it is accepted, once the user is known."""
    if change == RoleChange.REVOKE or user is None:
        return
    if not isinstance(scope, models.Call):
        return
    if role.name != RoleEnum.CALL_REVIEWER and not (
        role.template_id and role.template.name == RoleEnum.CALL_REVIEWER
    ):
        return
    reviewer_standing.ensure_reviewer_eligible(user, scope)


def _is_manager_role(role) -> bool:
    return role.name == RoleEnum.PROPOSAL_MANAGER


def _lock_proposal(proposal):
    """Serialise concurrent revocations of a proposal's managers.

    Two requests each revoking a different one of the last two managers
    would otherwise both see the other still active.
    """
    if transaction.get_connection().in_atomic_block:
        list(
            models.Proposal.objects.select_for_update()
            .filter(pk=proposal.pk)
            .values_list("pk", flat=True)
        )


def _would_lose_last_manager(proposal, user) -> bool:
    _lock_proposal(proposal)
    return (
        not UserRole.objects.filter(
            scope=proposal, is_active=True, role__name=RoleEnum.PROPOSAL_MANAGER
        )
        .exclude(user=user)
        .exists()
    )


def oversees_proposal_invitations(request, scope) -> bool:
    """Call managers and organisers invite to the proposals of their call.

    Which roles they may offer, and when, is up to
    ``guard_proposal_team_change``, as for direct grants.
    """
    return isinstance(
        scope, models.Proposal
    ) and proposal_permissions.oversees_proposal_call(request, scope)


def is_last_proposal_manager_role(user_role) -> bool:
    """Expiration-sweep guard: a proposal keeps its last manager.

    Expiry is not a decision anyone made, so instead of leaving the proposal
    without anyone to manage it the grant stays and the lapse is logged.
    """
    scope = user_role.scope
    if not isinstance(scope, models.Proposal) or not _is_manager_role(user_role.role):
        return False
    others = (
        UserRole.objects.filter(
            scope=scope, is_active=True, role__name=RoleEnum.PROPOSAL_MANAGER
        )
        .exclude(pk=user_role.pk)
        .exists()
    )
    if others:
        return False
    logger.warning(
        "Not expiring the role of %s as the last manager of proposal %s.",
        user_role.user,
        scope.uuid.hex,
    )
    return True


def _evaluates_proposal(user, proposal) -> bool:
    """Whether ``user`` evaluates ``proposal``, or may be asked to: a role on
    its call, an evaluator role on it, or a review of it."""
    call = proposal.round.call
    return (
        UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type=ContentType.objects.get_for_model(models.Call),
            object_id=call.pk,
        ).exists()
        or models.ProposalRoleAssignment.objects.filter(
            user=user, proposal=proposal, revoked_at__isnull=True
        ).exists()
        or models.Review.objects.filter(proposal=proposal, reviewer=user).exists()
    )


def guard_proposal_team_change(scope, role, acting_user, change, user=None):
    """Who may change a proposal's team, and when.

    - Nobody changes their own proposal role, staff included.
    - A proposal keeps at least one proposal manager: revoking the last one
      is refused, whoever asks.
    - The proposal manager and administrator roles are granted and revoked
      by the proposal's managers and by whoever oversees its call (call
      managers, call organisers), or staff. Other roles carrying the team
      permissions may still manage the rest of the team.
    - Once the proposal leaves draft, the team is part of what was submitted
      and is frozen for the applicant: only staff and those overseeing the
      call may change it, and the applicant while an open request for
      changes unlocks the team. Each such change is
      logged by the generic role event (see ``describe_proposal_role_event``).
      The applicant does not add someone who evaluates the call's proposals
      then: joining the team would end their evaluation of this one.
    - Once the award is accepted, the team is settled for everyone, staff
      included: the people are managed on the awarded project from then on.
    """
    if not isinstance(scope, models.Proposal):
        return
    if scope.state == enums.ProposalStates.ACCEPTED:
        raise ValidationError(AWARDED_TEAM_FROZEN_MESSAGE)
    # Unlike other scopes, where staff and organisation owners may, nobody
    # changes their own proposal role: the team is part of the application.
    # Creating a proposal makes its author a manager outside this path.
    if acting_user is not None and user is not None and acting_user.pk == user.pk:
        raise ValidationError(OWN_ROLE_MESSAGE)
    if (
        change == RoleChange.REVOKE
        and _is_manager_role(role)
        and user is not None
        and _would_lose_last_manager(scope, user)
    ):
        raise ValidationError(LAST_MANAGER_MESSAGE)

    if acting_user is not None and acting_user.is_staff:
        return

    managed_by_managers = role.name in TEAM_ROLES_MANAGED_BY_MANAGERS
    oversees = proposal_permissions.oversees_proposal_call(acting_user, scope)
    if scope.state != enums.ProposalStates.DRAFT:
        if oversees:
            return
        if not change_requests.applicant_may_edit(scope, enums.ChangeRequestParts.TEAM):
            raise ValidationError(TEAM_FROZEN_MESSAGE)
        # Joining the team ends one's evaluation of the proposal: the
        # applicant does not take an evaluator off it that way.
        if (
            change == RoleChange.GRANT
            and user is not None
            and _evaluates_proposal(user, scope)
        ):
            raise ValidationError(EVALUATOR_JOINS_MESSAGE)

    if (
        managed_by_managers
        and not oversees
        and not proposal_permissions.is_active_proposal_manager(acting_user, scope)
    ):
        raise ValidationError(ONLY_MANAGERS_MESSAGE)


def describe_proposal_role_event(user_role):
    """Enrich the generic role event of a proposal team change.

    A team change is logged once, as the generic role granted/updated/revoked
    event; on a proposal it also records the proposal's state and whether the
    change came after submission. Only staff and those overseeing the call get
    past the freeze, so each post-submission change is an override. An
    invitation or a permission request is attributed to the inviter or the
    approver, on whose authority it was admitted.
    """
    proposal = user_role.scope
    after_submission = proposal.state != enums.ProposalStates.DRAFT
    return RoleEventDetails(
        context={
            "proposal_state": proposal.state,
            "after_submission": after_submission,
        },
        role_label=user_role.role.description or user_role.role.name,
        scope_note="(after submission)" if after_submission else "",
    )


def is_submitted_proposal_role(user_role) -> bool:
    """Expiration-sweep guard (waldur_core.permissions.check_expired_permissions).

    A submitted proposal's team is part of the application record and feeds the
    awarded project's roles, so its grants must not be auto-revoked when their
    expiration_time passes. Draft proposals keep normal expiry, so a temporary
    drafting collaborator can still auto-lapse before submission.
    """
    scope = user_role.scope
    return (
        isinstance(scope, models.Proposal) and scope.state != enums.ProposalStates.DRAFT
    )


def create_checklist_completion(sender, instance, created, **kwargs):
    """Create checklist completion tracking when proposal is created."""
    if created and instance.round.call.compliance_checklist:
        proposal_content_type = ContentType.objects.get_for_model(instance)
        checklist_models.ChecklistCompletion.objects.create(
            scope_content_type=proposal_content_type,
            scope_object_id=instance.id,
            checklist=instance.round.call.compliance_checklist,
        )


def delete_checklist_completion(sender, instance, **kwargs):
    """Remove checklist completion tracking when proposal is deleted."""
    proposal_content_type = ContentType.objects.get_for_model(instance)
    checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=proposal_content_type,
        scope_object_id=instance.id,
    ).delete()


def seed_workflow_steps(sender, instance, created, **kwargs):
    """Seed the default workflow steps on call creation, in order.

    Only steps that have a working completion surface today — the
    call-manager-owned steps (administrative check, allocation decision) — are
    enabled by default, so a newly created call's workflow is fully drivable by
    the call manager end to end and needs no legacy Accept/Reject fallback. The
    remaining evaluation steps (offering-manager / reviewer / panel-member
    owned) are seeded *disabled*; a call manager can opt them in from the call
    config UI once their actor-facing surfaces exist, without stranding the
    workflow in the meantime.

    The award response is not seeded: the allocation decision's
    ``include_award_response`` adds it. The allocation decision is always
    enabled here: activation requires one.
    """
    if not created:
        return
    order = 0
    for default in enums.DEFAULT_CALL_STEPS:
        if default.key == enums.AWARD_RESPONSE_KEY:
            continue
        call_step, step_created = models.CallWorkflowStep.objects.get_or_create(
            call=instance,
            key=default.key,
            defaults={
                **call_step_values(default),
                "order": order,
                "is_enabled": default.completer == enums.ResponsibleRoles.CALL_MANAGER,
            },
        )
        order += 1
        if step_created:
            seed_notification_rules(call_step)


def call_step_values(default):
    """Field values of a call step made from one of ``DEFAULT_CALL_STEPS``."""
    return {
        "kind": default.kind,
        "name": default.name,
        "description": default.description,
        "outcome_set": default.outcome_set,
        "completer": default.completer,
        "on_negative": default.on_negative,
        "is_allocation_decision": default.is_allocation_decision,
        "contributor_source": default.contributor_source,
    }


def clear_panel_chair_on_role_revoked(sender, instance, **kwargs):
    """Drop ``Call.panel_chair`` or ``Call.panel_vice_chair`` when its holder
    loses the panel member role, and take them off the call's panels.

    ``UserRole.revoke`` is the single choke point for the Team tab's remove
    action, admin revocation and the expiry sweep, so one handler covers all.
    """
    if instance.role.name != RoleEnum.CALL_PANEL_MEMBER:
        return
    if not isinstance(instance.scope, models.Call):
        return
    models.Call.objects.filter(
        pk=instance.scope.pk, panel_chair_id=instance.user_id
    ).update(panel_chair=None)
    models.Call.objects.filter(
        pk=instance.scope.pk, panel_vice_chair_id=instance.user_id
    ).update(panel_vice_chair=None)
    # Nor do they sit on, or chair, any of the call's panels.
    panels.remove_from_call_panels(instance.scope, instance.user)


def end_evaluation_on_joining_team(sender, instance, current_user=None, **kwargs):
    """Whoever joins a proposal's applicant team stops evaluating it.

    The same treatment as a recusal for a conflict of interest: their
    evaluator role on the proposal is revoked, a review they are still
    writing is rejected, assignments not yet answered are blocked and their
    open contributions to its steps are withdrawn. The access checks refuse
    team members on their own (``proposal_roles.is_on_applicant_team``);
    this ends what they were given, so it shows as ended.

    ``UserRole`` grants and re-activations both send ``role_granted``,
    whether by a direct grant or an accepted invitation.
    """
    proposal = instance.scope
    if not isinstance(proposal, models.Proposal) or not instance.is_active:
        return
    user = instance.user
    for assignment in proposal_roles.revoke_on_joining_team(
        user, proposal, revoked_by=current_user
    ):
        transaction.on_commit(
            lambda uuid=assignment.uuid.hex: tasks.notify_proposal_role_changed.delay(
                uuid, False
            )
        )
    models.AssignmentItem.objects.filter(
        proposal=proposal,
        batch__reviewer_pool_entry__reviewer__user=user,
        status=enums.AssignmentItemStatuses.PENDING,
    ).update(status=enums.AssignmentItemStatuses.COI_BLOCKED, has_coi=True)
    models.Review.objects.filter(
        proposal=proposal,
        reviewer=user,
        state=models.Review.States.IN_REVIEW,
    ).update(
        state=models.Review.States.REJECTED,
        rejection_cause=enums.ReviewRejectionCauses.WITHDRAWN,
    )
    contributions.withdraw_open_of(user, proposal)


def settle_slots_on_conflict(sender, instance, created=False, **kwargs):
    """Settle the conflicted person's slots on the proposal's steps of an
    evaluator role or a call role, such as the panel's, once a conflict of
    interest with it is recorded, recused for, dismissed or waived.

    A conflict that stands stops their access to the proposal at once (see
    ``proposal_roles._effective``); this ends the open slots it gave, so they
    show as ended, and completes the step their slot was the last open one
    of. A conflict dismissed or waived, with none other standing, reopens
    them.
    """
    if not instance.proposal_id or not instance.reviewer_id:
        return
    proposal = instance.proposal
    user = instance.reviewer.user
    completed, next_instance = workflow_service.settle_evaluator_slots(
        proposal, user, instance.reviewed_by
    )
    if completed:
        transaction.on_commit(
            lambda: tasks.notify_contribution_decision(
                proposal, completed, next_instance
            )
        )


def end_evaluator_roles_no_longer_eligible(call, user_ids, revoked_by=None):
    """End the evaluator roles the people ``user_ids`` hold on ``call``'s
    proposals through a call role or a panel they no longer hold: the
    assignment is revoked, logged and told of, the lead's slots follow, and
    their slots on the proposals' steps are settled as on any revocation."""
    with transaction.atomic():
        revoked = proposal_roles.revoke_no_longer_eligible(call, user_ids, revoked_by)
        for assignment in revoked:
            proposal = assignment.proposal
            activity.log_evaluator_revoked(assignment)
            contributions.sync_lead_slots(proposal, assignment.role)
            completed, next_instance = workflow_service.settle_evaluator_slots(
                proposal, assignment.user, revoked_by
            )
            if completed:
                transaction.on_commit(
                    lambda proposal=proposal,
                    completed=completed,
                    next_instance=next_instance: (
                        tasks.notify_contribution_decision(
                            proposal, completed, next_instance
                        )
                    )
                )
            transaction.on_commit(
                lambda uuid=assignment.uuid.hex: (
                    tasks.notify_proposal_role_changed.delay(uuid, False)
                )
            )
    return revoked


def end_evaluator_roles_on_call_role_revoked(
    sender, instance, current_user=None, **kwargs
):
    """Whoever loses a role on a call -- taken away, or lapsed and swept by
    the expiry task -- loses the evaluator roles they held through it, or
    through the call's panels they were taken off with it."""
    call = instance.scope
    if not isinstance(call, models.Call) or instance.is_active:
        return
    end_evaluator_roles_no_longer_eligible(
        call, [instance.user_id], revoked_by=current_user
    )


def end_evaluator_roles_on_leaving_panel(
    sender, instance, action, reverse, pk_set, **kwargs
):
    """Whoever is taken off a call's panel loses the evaluator roles they
    held through it, on the proposals routed to it."""
    # Members are changed from the panel's side only: the relation has no
    # reverse accessor on the user.
    if action != "post_remove" or reverse or not pk_set:
        return
    end_evaluator_roles_no_longer_eligible(instance.call, list(pk_set))


def remember_panel_chair(sender, instance, **kwargs):
    """Note the chair a panel had before it is saved, for
    ``end_evaluator_roles_on_chair_change``."""
    instance._previous_chair_id = (
        models.CallPanel.objects.filter(pk=instance.pk)
        .values_list("chair_id", flat=True)
        .first()
        if instance.pk
        else None
    )


def end_evaluator_roles_on_chair_change(sender, instance, created=False, **kwargs):
    """A panel's chair replaced, or cleared, loses the roles held as its
    chair on the proposals routed to it."""
    previous = getattr(instance, "_previous_chair_id", None)
    if created or previous is None or previous == instance.chair_id:
        return
    end_evaluator_roles_no_longer_eligible(instance.call, [previous])


def settle_slots_on_call_role_revoked(sender, instance, current_user=None, **kwargs):
    """Whoever loses a role on a call -- taken away, or lapsed and swept by
    the expiry task, both through ``UserRole.revoke`` -- loses the open slots
    it gave them on the call's proposals: those of the steps of the role's
    holders. They lapse as an evaluator role's do, and the step their slot
    was the last open one of is completed (see ``settle_slots_on_conflict``).
    """
    call = instance.scope
    if not isinstance(call, models.Call) or instance.is_active:
        return
    user = instance.user
    proposal_ids = set(
        models.StepContribution.objects.filter(
            contributor=user,
            requested_offering__isnull=True,
            review__isnull=True,
            status__in=enums.StepContributionStatuses.OPEN,
            proposal_step__proposal__round__call=call,
            proposal_step__call_step__contributor_source__startswith=(
                f"{enums.ContributorSources.CALL_ROLE}:"
            ),
        ).values_list("proposal_step__proposal_id", flat=True)
    )
    for proposal in models.Proposal.objects.filter(pk__in=proposal_ids):
        completed, next_instance = workflow_service.settle_evaluator_slots(
            proposal, user, current_user
        )
        if completed:
            transaction.on_commit(
                lambda proposal=proposal,
                completed=completed,
                next_instance=(next_instance): (
                    tasks.notify_contribution_decision(
                        proposal, completed, next_instance
                    )
                )
            )


def seed_notification_rules(call_step):
    """Create the default notification rules for a freshly created step."""
    notification_rules.seed_default_rules(call_step)


# Proposal role -> project role a new call starts with: who the allocated
# project's team is made of. The awarded-project roles act as project manager,
# administrator and member, but leave the allocation to the call's workflow.
# A call manager edits them per call afterwards.
DEFAULT_PROJECT_ROLE_MAPPINGS = (
    (RoleEnum.PROPOSAL_MANAGER, RoleEnum.PROJECT_PRINCIPAL_INVESTIGATOR),
    (RoleEnum.PROPOSAL_ADMIN, RoleEnum.PROJECT_CO_INVESTIGATOR),
    (RoleEnum.PROPOSAL_MEMBER, RoleEnum.PROJECT_COLLABORATOR),
)


def seed_project_role_mappings(sender, instance, created, **kwargs):
    """Give a new call the default proposal-to-project role mappings.

    Only at creation, so calls that exist already keep theirs. A duplicate
    or an import that brings its own mappings replaces these.
    """
    if not created:
        return
    proposal_type = ContentType.objects.get_for_model(models.Proposal)
    project_type = ContentType.objects.get_by_natural_key("structure", "project")
    for proposal_role, project_role in DEFAULT_PROJECT_ROLE_MAPPINGS:
        models.ProposalProjectRoleMapping.objects.get_or_create(
            call=instance,
            proposal_role=Role.objects.get_system_role(
                proposal_role, content_type=proposal_type
            ),
            defaults={
                "project_role": Role.objects.get_system_role(
                    project_role, content_type=project_type
                )
            },
        )


def seed_proposal_field_config(sender, instance, created, **kwargs):
    """Materialise a call's Project details field configuration at creation.

    Deliberately a stored row rather than a lazy read of the Constance defaults:
    a call that resolved its defaults on every read would tighten retroactively
    the moment an operator added a field to DEFAULT_PROPOSAL_REQUIRED_FIELDS,
    invalidating drafts written under the old form. Seeding once means the
    installation default is a starting point, never a later imposition.
    """
    if not created:
        return
    states = models.CallProposalFieldConfig.default_states()
    columns = {
        models.CallProposalFieldConfig.column_for(field_name): state
        for field_name, state in states.items()
    }
    models.CallProposalFieldConfig.objects.get_or_create(
        call=instance, defaults=columns
    )


def attach_review_contribution(sender, instance, created=False, **kwargs):
    """Give every review a contribution on the step that collects reviews.

    Whichever way the review came about -- an accepted assignment, a call
    manager's direct assignment, an import -- so the step's gates count it.
    """
    contributions.attach_review(instance)


def attach_assignment_contribution(sender, instance, created=False, **kwargs):
    """Link an accepted assignment to its review's contribution.

    The contribution moves to the step the assignment's batch was made for.
    """
    if instance.status != enums.AssignmentItemStatuses.ACCEPTED or not (
        instance.review_id
    ):
        return
    # Idempotent: a contribution already linked is left as it is.
    contributions.attach_review(instance.review, assignment_item=instance)


def delete_contribution_completions(sender, instance, **kwargs):
    """Remove the answers kept for a contribution along with it."""
    checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=ContentType.objects.get_for_model(instance),
        scope_object_id=instance.id,
    ).delete()


def notify_provider_about_offering_request(sender, instance, created=False, **kwargs):
    """A call asks to include an offering: tell its provider side, once the
    request is committed."""
    if not created or instance.state != models.RequestedOffering.States.REQUESTED:
        return
    # A loader may set the uuid as a plain string rather than a UUID.
    uuid = str(instance.uuid).replace("-", "")
    transaction.on_commit(
        lambda: tasks.notify_provider_about_offering_request.delay(uuid)
    )


def notify_provider_about_changed_terms(sender, instance, **kwargs):
    """The call changed a limit or a pool of an offering its provider has
    accepted: the offering's managers are told what the call now asks."""
    rule = (
        instance
        if isinstance(instance, models.CallOfferingComponent)
        else getattr(instance, "offering_component", None)
    )
    requested_offering = getattr(rule, "requested_offering", None)
    # A dry run copies the call's rules onto offerings nobody provides.
    if dry_run_context.current() is not None:
        return
    if (
        requested_offering is None
        or requested_offering.state != RequestedOfferingStates.ACCEPTED
    ):
        return
    uuid_hex = requested_offering.uuid.hex
    transaction.on_commit(
        lambda: tasks.notify_call_offering_terms_changed.delay(uuid_hex)
    )
