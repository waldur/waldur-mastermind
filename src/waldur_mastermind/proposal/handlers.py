"""Signal handlers for proposal application."""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework.exceptions import ValidationError

from waldur_core.checklist import models as checklist_models
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import RoleChange, RoleEventDetails
from waldur_mastermind.proposal import enums, models
from waldur_mastermind.proposal import permissions as proposal_permissions

logger = logging.getLogger(__name__)

TEAM_FROZEN_MESSAGE = (
    "The proposal team cannot be changed once the proposal is submitted. "
    "Ask a call manager to change it."
)
ONLY_MANAGERS_MESSAGE = (
    "Only a proposal manager or a call manager may grant or revoke the "
    "proposal manager and proposal administrator roles."
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
      call may change it. Each such change is
      logged by the generic role event (see ``describe_proposal_role_event``).
    """
    if not isinstance(scope, models.Proposal):
        return
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
        raise ValidationError(TEAM_FROZEN_MESSAGE)

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
    """Seed catalog workflow steps on call creation.

    Only steps that have a working completion surface today — the
    call-manager-owned steps (administrative check, allocation decision) — are
    enabled by default, so a newly created call's workflow is fully drivable by
    the call manager end to end and needs no legacy Accept/Reject fallback. The
    remaining evaluation steps (offering-manager / reviewer / panel-member
    owned) are seeded *disabled*; a call manager can opt them in from the call
    config UI once their actor-facing surfaces exist, without stranding the
    workflow in the meantime.

    ``award_response`` is intentionally excluded: it is provisioned via
    ``allocation_decision.include_award_response`` and direct creation is
    blocked by the serializer. Mandatory steps (allocation_decision) cannot be
    disabled and are always enabled here.
    """
    if not created:
        return
    for step_def in enums.WORKFLOW_STEPS:
        if step_def.id == "award_response":
            continue
        enabled = (
            step_def.default_responsible_role == enums.ResponsibleRoles.CALL_MANAGER
        )
        call_step, step_created = models.CallWorkflowStep.objects.get_or_create(
            call=instance,
            step=step_def.id,
            defaults={"is_enabled": enabled},
        )
        if step_created:
            seed_notification_rules(call_step)


# Default rules: internal steps warn the responsible role and the call
# manager a day before expiry and again on expiry; the applicant is never told
# about internal steps, and by default is not told that evaluation is
# progressing either (a manager can opt in with a step_started → applicant rule
# on allocation_decision). The award response reminds the applicant before it
# lapses. Keyed by step id; ``None`` applies to every step.
DEFAULT_NOTIFICATION_RULES = {
    None: [
        (
            enums.NotificationRuleTriggers.DEADLINE_APPROACHING,
            enums.NotificationRuleRecipients.RESPONSIBLE_ROLE,
            1,
        ),
        (
            enums.NotificationRuleTriggers.DEADLINE_APPROACHING,
            enums.NotificationRuleRecipients.CALL_MANAGERS,
            1,
        ),
        (
            enums.NotificationRuleTriggers.STEP_EXPIRED,
            enums.NotificationRuleRecipients.RESPONSIBLE_ROLE,
            None,
        ),
        (
            enums.NotificationRuleTriggers.STEP_EXPIRED,
            enums.NotificationRuleRecipients.CALL_MANAGERS,
            None,
        ),
    ],
    # The chair owns the panel's consolidated recommendation, so they get the
    # same reminders as the members (resolved through responsible_role above).
    "panel_review": [
        (
            enums.NotificationRuleTriggers.DEADLINE_APPROACHING,
            enums.NotificationRuleRecipients.PANEL_CHAIR,
            1,
        ),
        (
            enums.NotificationRuleTriggers.STEP_EXPIRED,
            enums.NotificationRuleRecipients.PANEL_CHAIR,
            None,
        ),
    ],
    "award_response": [
        (
            enums.NotificationRuleTriggers.STEP_STARTED,
            enums.NotificationRuleRecipients.APPLICANT,
            None,
        ),
        (
            enums.NotificationRuleTriggers.DEADLINE_APPROACHING,
            enums.NotificationRuleRecipients.APPLICANT,
            1,
        ),
    ],
}


def clear_panel_chair_on_role_revoked(sender, instance, **kwargs):
    """Drop ``Call.panel_chair`` when the chair loses the panel member role.

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


def seed_notification_rules(call_step):
    """Create the default notification rules for a freshly created step."""
    rules = DEFAULT_NOTIFICATION_RULES[None] + DEFAULT_NOTIFICATION_RULES.get(
        call_step.step, []
    )
    for trigger, recipient, days_before in rules:
        models.CallWorkflowStepNotificationRule.objects.get_or_create(
            workflow_step=call_step,
            trigger=trigger,
            recipient=recipient,
            defaults={"days_before": days_before},
        )


# Proposal role -> project role a new call starts with: who the allocated
# project's team is made of. A call manager edits them per call afterwards.
DEFAULT_PROJECT_ROLE_MAPPINGS = (
    (RoleEnum.PROPOSAL_MANAGER, RoleEnum.PROJECT_MANAGER),
    (RoleEnum.PROPOSAL_ADMIN, RoleEnum.PROJECT_ADMIN),
    (RoleEnum.PROPOSAL_MEMBER, RoleEnum.PROJECT_MEMBER),
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
