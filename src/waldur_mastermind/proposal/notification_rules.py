"""Call-level workflow notification rules: resolution and dispatch.

A ``CallWorkflowStepNotificationRule`` says *on this event, for this step,
mail this audience*. This module turns a rule into concrete users for a
concrete proposal, and is the single place the workflow engine calls when a
step changes status. Sending itself happens in ``tasks.notify_workflow_step_event``
after the surrounding transaction commits, so a rolled-back transition never
produces mail.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from waldur_core.core.models import User
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    RequestedOfferingStates,
    ResponsibleRoles,
)
from waldur_mastermind.proposal.permissions import (
    RESPONSIBLE_ROLE_TO_CALL_ROLE,
    offering_manager_user_ids_for_proposal,
)

logger = logging.getLogger(__name__)

# Steps whose progress the applicant is not told about: internal
# evaluation. The applicant only ever hears about the decision and the award.
INTERNAL_STEPS = frozenset(
    {"administrative_check", "technical_assessment", "expert_review", "panel_review"}
)


def ledger_key(trigger, days_before=None):
    if trigger == NotificationRuleTriggers.DEADLINE_APPROACHING:
        return f"{trigger}:{days_before}"
    return trigger


def _call_offering_manager_users(call):
    """Managers of every offering accepted into the call.

    For the call manager's configuration view only, which answers "who could
    evaluate on this call". Mail about a proposal goes to that proposal's
    offering managers instead; see ``_responsible_role_users``.
    """
    offering_ids = models.RequestedOffering.objects.filter(
        call=call, state=RequestedOfferingStates.ACCEPTED
    ).values_list("offering_id", flat=True)
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    user_ids = permissions_models.UserRole.objects.filter(
        is_active=True,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=offering_ct,
        object_id__in=offering_ids,
    ).values_list("user_id", flat=True)
    return User.objects.filter(id__in=user_ids)


def _responsible_role_users(call_step, proposal=None):
    """Users holding the step's responsible role for ``proposal``.

    The applicant and offering-manager roles are per proposal: the offering
    managers are those of the offerings the proposal requested, the same set
    that may act on the step. Without a proposal both resolve to nobody.
    """
    role = call_step.responsible_role
    call = call_step.call
    if not role:
        return User.objects.none()
    if role == ResponsibleRoles.APPLICANT:
        if proposal is None:
            return User.objects.none()
        return _applicant_users(proposal)
    if role == ResponsibleRoles.OFFERING_MANAGER:
        if proposal is None:
            return User.objects.none()
        return User.objects.filter(
            id__in=offering_manager_user_ids_for_proposal(proposal)
        )
    role_name = RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if not role_name:
        return User.objects.none()
    return get_users(call, role_name=role_name)


def _applicant_users(proposal, roles=()):
    """The applicant side of a proposal.

    Holders of ``roles`` on the proposal when given; otherwise the proposal
    creator plus everyone holding a role on the proposal.
    """
    if roles:
        ids = permissions_models.UserRole.objects.filter(
            scope=proposal, is_active=True, role__in=roles
        ).values_list("user_id", flat=True)
        return User.objects.filter(id__in=ids)
    ids = set(get_users(proposal).values_list("id", flat=True))
    if proposal.created_by_id:
        ids.add(proposal.created_by_id)
    return User.objects.filter(id__in=ids)


def _assigned_reviewer_users(proposal):
    reviewer_ids = proposal.review_set.exclude(reviewer__isnull=True).values_list(
        "reviewer_id", flat=True
    )
    return User.objects.filter(id__in=reviewer_ids)


def responsible_users_for_step(call_step):
    """Who will act on this step, for the call manager's configuration view.

    Includes offering managers of offerings the provider has accepted into
    the call (technical assessment): that acceptance is the working
    relationship that justifies showing provider staff to the call manager.
    """
    if call_step.responsible_role == ResponsibleRoles.OFFERING_MANAGER:
        users = _call_offering_manager_users(call_step.call)
    else:
        users = _responsible_role_users(call_step)
    return users.filter(is_active=True).order_by("first_name", "last_name", "username")


def resolve_recipients(rule, proposal):
    """Users addressed by ``rule`` for ``proposal``, mail-eligible only."""
    recipient = rule.recipient
    call_step = rule.workflow_step
    call = call_step.call
    if is_applicant_audience(rule, proposal):
        users = _applicant_users(proposal, list(rule.notified_proposal_roles.all()))
    elif recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE:
        users = _responsible_role_users(call_step, proposal)
    elif recipient == NotificationRuleRecipients.ASSIGNED_REVIEWERS:
        users = _assigned_reviewer_users(proposal)
    elif recipient == NotificationRuleRecipients.CALL_MANAGERS:
        users = call.call_managers
    elif recipient == NotificationRuleRecipients.ALL_POOL_REVIEWERS:
        users = call.reviewers
    elif recipient == NotificationRuleRecipients.PANEL_CHAIR:
        # An unset chair resolves to nobody; the rule is simply inert.
        users = User.objects.filter(id=call.panel_chair_id)
    else:
        logger.warning("Unknown notification rule recipient %s", recipient)
        return User.objects.none()
    return (
        users.filter(is_active=True)
        .exclude(email="")
        .exclude(notifications_enabled=False)
    )


def is_applicant_recipient(recipient, call_step):
    """True when ``recipient`` on ``call_step`` is the applicant side."""
    if recipient == NotificationRuleRecipients.APPLICANT:
        return True
    return (
        recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE
        and call_step.responsible_role == ResponsibleRoles.APPLICANT
    )


def is_applicant_audience(rule, proposal):
    """True when the mail goes to the applicant side and must stay status-only.

    Such a rule reaches the proposal roles it names (all of the team and the
    creator by default), every recipient in one message.
    """
    return is_applicant_recipient(rule.recipient, rule.workflow_step)


def enabled_rules(instance, trigger):
    """Enabled rules of the proposal's call matching this instance's step + trigger."""
    return (
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=instance.proposal.round.call,
            workflow_step__step=instance.step,
            trigger=trigger,
            is_enabled=True,
        )
        .select_related("workflow_step", "workflow_step__call")
        .prefetch_related("notified_proposal_roles")
    )


def dispatch_step_event(instance, trigger):
    """Queue mail for ``trigger`` on ``instance`` once the transaction commits.

    Cheap no-op when the call has no enabled rule for the event, so the
    workflow engine can call it unconditionally. Status-change triggers are
    recorded in ``sent_notifications`` here (inside the caller's transaction)
    so a retried transition cannot double-send.
    """
    if not enabled_rules(instance, trigger).exists():
        return
    key = ledger_key(trigger)
    if key in instance.sent_notifications:
        return
    instance.sent_notifications = [*instance.sent_notifications, key]
    instance.save(update_fields=["sent_notifications"])

    from waldur_mastermind.proposal import tasks

    instance_uuid = instance.uuid.hex
    transaction.on_commit(
        lambda: tasks.notify_workflow_step_event.delay(instance_uuid, trigger)
    )
