"""Signal handlers for proposal application."""

from django.contrib.contenttypes.models import ContentType

from waldur_core.checklist import models as checklist_models
from waldur_core.permissions.enums import RoleEnum
from waldur_mastermind.proposal import enums, models


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
