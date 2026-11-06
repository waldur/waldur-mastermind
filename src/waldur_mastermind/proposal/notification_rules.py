"""Call-level workflow notification rules: resolution and dispatch.

A ``CallWorkflowStepNotificationRule`` says *on this event, for this step,
mail this audience*. This module turns a rule into concrete users for a
concrete proposal, and is the single place the workflow engine calls when a
step changes status. Sending itself happens in ``tasks.notify_workflow_step_event``
after the surrounding transaction commits, so a rolled-back transition never
produces mail.
"""

import logging
from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from waldur_core.core.models import User
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import dry_run_context, managers, models, proposal_roles
from waldur_mastermind.proposal.enums import (
    COMMON_NOTIFICATION_RULES,
    PANEL_CHAIR_NOTIFICATION_RULES,
    ContributorSources,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    RequestedOfferingStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    StepContributionStatuses,
    parse_contributor_source,
)
from waldur_mastermind.proposal.permissions import (
    RESPONSIBLE_ROLE_TO_CALL_ROLE,
    offering_manager_user_ids_for_proposal,
    offering_team_user_ids_for_proposal,
    panel_chair_ids,
)

logger = logging.getLogger(__name__)


# Whether the applicant is kept out of a step's progress, and whether a rule's
# recipient is the applicant side; the rule model validates against them too.
is_internal_step = models.is_internal_step
is_applicant_recipient = models.is_applicant_recipient


def default_rules(call_step):
    """``(trigger, recipient, days_before)`` rules a new step starts with.

    Every step warns its completer and the call managers before it lapses
    and when it does; its kind adds its own (the applicant is reminded of a
    step that is theirs), and a panel step involves the panel chair. By
    default the applicant is not told that evaluation is progressing; a
    manager can opt in with a step_started → applicant rule on the allocation
    decision.
    """
    rules = list(COMMON_NOTIFICATION_RULES)
    kind = call_step.kind_definition
    if kind:
        rules += kind.default_notification_rules
    if call_step.completer == ResponsibleRoles.PANEL_MEMBER:
        # The chair is the completer of a panel_chair step: its own
        # responsible_role rules address them already.
        rules += PANEL_CHAIR_NOTIFICATION_RULES
    return rules


def seed_default_rules(call_step):
    """Create the default notification rules for a freshly created step."""
    for trigger, recipient, days_before in default_rules(call_step):
        models.CallWorkflowStepNotificationRule.objects.get_or_create(
            workflow_step=call_step,
            trigger=trigger,
            recipient=recipient,
            defaults={"days_before": days_before},
        )


def ledger_key(trigger, days_before=None):
    if trigger == NotificationRuleTriggers.DEADLINE_APPROACHING:
        return f"{trigger}:{days_before}"
    return trigger


def _call_offering_manager_roles(call):
    """The offering manager roles held on the offerings accepted into the
    call."""
    offering_ids = models.RequestedOffering.objects.filter(
        call=call, state=RequestedOfferingStates.ACCEPTED
    ).values_list("offering_id", flat=True)
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=offering_ct,
        object_id__in=offering_ids,
    )


def _call_offering_manager_users(call):
    """Managers of every offering accepted into the call.

    For the call manager's configuration view only, which answers "who could
    evaluate on this call". Mail about a proposal goes to that proposal's
    offering managers instead; see ``_responsible_role_users``.
    """
    user_ids = _call_offering_manager_roles(call).values_list("user_id", flat=True)
    return User.objects.filter(id__in=user_ids)


def call_offerings_by_manager(call):
    """``{user_id: [offering, ...]}``: the offerings accepted into the call
    that each of their managers manages, by name. Tells the call manager
    which provider stands behind each offering manager."""
    pairs = list(_call_offering_manager_roles(call).values_list("user_id", "object_id"))
    offerings = marketplace_models.Offering.objects.filter(
        id__in={offering_id for _, offering_id in pairs}
    ).select_related("customer")
    offerings = {offering.id: offering for offering in offerings}
    result = defaultdict(list)
    for user_id, offering_id in pairs:
        if offering_id in offerings:
            result[user_id].append(offerings[offering_id])
    for user_offerings in result.values():
        user_offerings.sort(key=lambda offering: offering.name)
    return result


def _responsible_role_users(call_step, proposal=None):
    """Users holding the step's responsible role for ``proposal``.

    The applicant, offering-manager and evaluator roles are per proposal: the
    offering managers are those of the offerings the proposal requested, the
    evaluators those completing the step through its evaluator role
    (``proposal_roles.step_completers``: its lead alone on a lead-only step
    the role completes) -- the same set that may act on the step. Without a
    proposal they resolve to nobody.
    """
    role = call_step.completer
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
            id__in=offering_manager_user_ids_for_proposal(proposal, call_step)
        )
    if role == ResponsibleRoles.PROPOSAL_ROLE:
        if proposal is None:
            return User.objects.none()
        return proposal_roles.step_completers(proposal, call_step)
    if role == ResponsibleRoles.PANEL_CHAIR:
        return User.objects.filter(id__in=panel_chair_ids(call, proposal))
    role_name = RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if not role_name:
        return User.objects.none()
    users = get_users(call, role_name=role_name)
    if proposal is not None and role_name != RoleEnum.CALL_MANAGER:
        # A conflict of interest with the proposal keeps a call role's
        # holder off it (``permissions.holds_call_role_on_proposal``).
        users = users.exclude(
            id__in=proposal_roles.users_with_recorded_conflicts(proposal)
        )
    if proposal is not None and role_name == RoleEnum.CALL_REVIEWER:
        # The reviewer role is the call's pool: of it, only the reviewers
        # holding work on the proposal act on its step.
        users = users.filter(
            id__in=[
                user.pk
                for user in users
                if managers.holds_reviewer_work(user, proposal)
            ]
        )
    return users


def _applicant_users(proposal, roles=()):
    """The applicant side of a proposal.

    Holders of ``roles`` on the proposal when given; otherwise everyone on
    its team now. An author removed from the team is no longer told.
    """
    if roles:
        ids = permissions_models.UserRole.objects.filter(
            scope=proposal, is_active=True, role__in=roles
        ).values_list("user_id", flat=True)
        return User.objects.filter(id__in=ids)
    return get_users(proposal)


applicant_users = _applicant_users


def applicant_cc_emails(proposal, addressed=()):
    """Addresses copied on a mail to the applicant about ``proposal``: the
    holders of the proposal roles its call copies (``Call.applicant_cc_roles``)
    -- the principal investigator, the proposal administrators -- on the
    proposal's team now, mail-eligible, and not already among ``addressed``.
    Someone removed from the team is no longer copied."""
    roles = proposal.round.call.applicant_cc_roles or []
    if not roles:
        return []
    ids = permissions_models.UserRole.objects.filter(
        scope=proposal, is_active=True, role__name__in=roles
    ).values_list("user_id", flat=True)
    emails = (
        User.objects.filter(id__in=ids, is_active=True)
        .exclude(email="")
        .exclude(notifications_enabled=False)
        .values_list("email", flat=True)
    )
    seen = {address.lower() for address in addressed if address}
    result = []
    for email in sorted(set(emails)):
        if email.lower() not in seen:
            seen.add(email.lower())
            result.append(email)
    return result


# Who a call manager's own message to a proposal reaches.
MANAGER_MESSAGE_APPLICANT = "applicant"
MANAGER_MESSAGE_TEAM = "team"


def manager_message_emails(proposal, recipients):
    """``(to, cc)`` of a call manager's message about ``proposal``.

    ``applicant``: the proposal's creator while still on its team, else its
    managers, copied to the proposal roles the call copies on mail to the
    applicant. ``team``: everyone on its team now. Mail-eligible addresses
    only; someone removed from the team is no longer told.
    """
    team = _applicant_users(proposal)
    if recipients == MANAGER_MESSAGE_TEAM:
        users = team
    else:
        creator = proposal.created_by
        users = team.filter(pk=creator.pk) if creator else team.none()
        if not users.exists():
            users = team.filter(
                id__in=get_users(proposal, role_name=RoleEnum.PROPOSAL_MANAGER).values(
                    "id"
                )
            )
    to = sorted(
        set(
            users.filter(is_active=True)
            .exclude(email="")
            .exclude(notifications_enabled=False)
            .values_list("email", flat=True)
        )
    )
    if not to or recipients == MANAGER_MESSAGE_TEAM:
        return to, []
    return to, applicant_cc_emails(proposal, to)


responsible_users = _responsible_role_users


def _offering_team_users(proposal):
    """Managers of the offerings the proposal involves now, the hosting
    entities: requested until the award is decided, awarded after it
    (``permissions.offering_ids_in_play``)."""
    return User.objects.filter(id__in=offering_team_user_ids_for_proposal(proposal))


def _assigned_reviewer_users(proposal):
    """The reviewers of the proposal's live reviews: a review cancelled,
    expired or rejected no longer makes its reviewer one."""
    reviewer_ids = (
        proposal.review_set.exclude(reviewer__isnull=True)
        .exclude(state=models.Review.States.REJECTED)
        .values_list("reviewer_id", flat=True)
    )
    return User.objects.filter(id__in=reviewer_ids)


def responsible_users_for_step(call_step):
    """Who will act on this step, for the call manager's configuration view.

    Includes offering managers of offerings the provider has accepted into
    the call (technical assessment): that acceptance is the working
    relationship that justifies showing provider staff to the call manager.
    """
    if call_step.completer == ResponsibleRoles.OFFERING_MANAGER:
        users = _call_offering_manager_users(call_step.call)
    else:
        users = _responsible_role_users(call_step)
    return users.filter(is_active=True).order_by("first_name", "last_name", "username")


def contributing_users_for_step(call_step):
    """Who the step's contributor source names on the call, for the call
    manager's configuration view.

    The holders of the call role it names; the reviewers who accepted to
    join the call's reviewer pool, of whom each proposal's reviewers are
    assigned; or the managers of the offerings accepted into the call. An
    evaluator role is held per proposal, so it names nobody here, and
    neither does a step without contributors.
    """
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return User.objects.none()
    call = call_step.call
    if source == ContributorSources.REQUESTED_OFFERING_TEAM:
        users = _call_offering_manager_users(call)
    elif source == ContributorSources.ASSIGNED_REVIEWERS:
        users = User.objects.filter(
            reviewer_profile__pool_memberships__call=call,
            reviewer_profile__pool_memberships__invitation_status=(
                ReviewerPoolInvitationStatuses.ACCEPTED
            ),
        ).distinct()
    elif source == ContributorSources.CALL_ROLE:
        if parameter == ResponsibleRoles.PANEL_CHAIR:
            users = User.objects.filter(id__in=panel_chair_ids(call))
        else:
            users = get_users(
                call, role_name=proposal_roles.CONTRIBUTOR_CALL_ROLES[parameter]
            )
    else:
        return User.objects.none()
    return users.filter(is_active=True).order_by("first_name", "last_name", "username")


def resolve_recipients(rule, proposal):
    """Users addressed by ``rule`` for ``proposal``, mail-eligible only.

    The applicant is never told of an internal step, whatever the rule says:
    a step's completer or kind may have changed since the rule was made.
    Every other audience is the evaluation's side: nobody on the proposal's
    applicant team is in it, whatever role they also hold on the call, nor
    anyone a conflict of interest with the proposal stands against but the
    call managers.
    """
    recipient = rule.recipient
    call_step = rule.workflow_step
    call = call_step.call
    if is_applicant_audience(rule, proposal):
        if is_internal_step(call_step):
            return User.objects.none()
        return _mail_eligible(
            _applicant_users(proposal, list(rule.notified_proposal_roles.all()))
        )
    if recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE:
        users = _responsible_role_users(call_step, proposal)
    elif recipient == NotificationRuleRecipients.ASSIGNED_REVIEWERS:
        users = _assigned_reviewer_users(proposal)
    elif recipient == NotificationRuleRecipients.CALL_MANAGERS:
        users = call.call_managers
    elif recipient == NotificationRuleRecipients.ALL_POOL_REVIEWERS:
        users = call.reviewers
    elif recipient == NotificationRuleRecipients.PANEL_CHAIR:
        # The chair and the vice-chair; unset, the rule is simply inert.
        users = User.objects.filter(id__in=panel_chair_ids(call, proposal))
    elif recipient in NotificationRuleRecipients.NAMING_PROPOSAL_ROLE:
        users = (
            proposal_roles.proposal_role_holders(
                proposal,
                rule.proposal_role_id,
                lead_only=recipient == NotificationRuleRecipients.PROPOSAL_ROLE_LEAD,
            )
            if rule.proposal_role_id
            else User.objects.none()
        )
    elif recipient == NotificationRuleRecipients.OFFERING_TEAMS:
        users = _offering_team_users(proposal)
    else:
        logger.warning("Unknown notification rule recipient %s", recipient)
        return User.objects.none()
    users = users.exclude(id__in=proposal_roles.applicant_team_user_ids(proposal))
    if not _addresses_call_managers(rule):
        users = users.exclude(
            id__in=proposal_roles.users_with_recorded_conflicts(proposal)
        )
    return _mail_eligible(users)


def _addresses_call_managers(rule):
    """Whether ``rule`` mails the call's managers, whom a conflict of
    interest does not keep off a proposal of their call."""
    if rule.recipient == NotificationRuleRecipients.CALL_MANAGERS:
        return True
    return (
        rule.recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE
        and rule.workflow_step.completer == ResponsibleRoles.CALL_MANAGER
    )


def _mail_eligible(users):
    return (
        users.filter(is_active=True)
        .exclude(email="")
        .exclude(notifications_enabled=False)
    )


def is_applicant_audience(rule, proposal):
    """True when the mail goes to the applicant side and must stay status-only.

    Such a rule reaches the proposal roles it names (all of the team and the
    creator by default), every recipient in one message.
    """
    return is_applicant_recipient(rule.recipient, rule.workflow_step)


def enabled_rules(instance, trigger):
    """Enabled rules of this instance's call step matching the trigger."""
    return (
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step_id=instance.call_step_id,
            trigger=trigger,
            is_enabled=True,
        )
        .select_related("workflow_step", "workflow_step__call", "proposal_role")
        .prefetch_related("notified_proposal_roles")
    )


def dispatch_step_event(instance, trigger, occurrence=None):
    """Queue mail for ``trigger`` on ``instance`` once the transaction commits.

    Cheap no-op when the call has no enabled rule for the event, so the
    workflow engine can call it unconditionally. Status-change triggers are
    recorded in ``sent_notifications`` here (inside the caller's transaction)
    so a retried transition cannot double-send. An event that may happen on
    a step more than once (``NotificationRuleTriggers.REPEATABLE``) is
    recorded per ``occurrence``.
    """
    if not enabled_rules(instance, trigger).exists():
        return
    key = ledger_key(trigger)
    if occurrence:
        key = f"{key}:{occurrence}"
    if key in instance.sent_notifications:
        return
    instance.sent_notifications = [*instance.sent_notifications, key]
    instance.save(update_fields=["sent_notifications"])

    collector = dry_run_context.current()
    if collector is not None:
        # A dry run renders the mail where the task would send it.
        collector.step_event(instance, trigger)
        return

    from waldur_mastermind.proposal import tasks

    instance_uuid = instance.uuid.hex
    transaction.on_commit(
        lambda: tasks.notify_workflow_step_event.delay(instance_uuid, trigger)
    )


def dispatch_contribution_submitted(contribution):
    """Queue the step's ``contribution_submitted`` mail for one contribution
    handed in: each submission is announced, a contribution handed in again
    after it was reopened included."""
    if contribution.status != StepContributionStatuses.SUBMITTED:
        return
    submitted_at = contribution.submitted_at
    occurrence = contribution.uuid.hex
    if submitted_at is not None:
        occurrence = f"{occurrence}:{int(submitted_at.timestamp() * 1000)}"
    dispatch_step_event(
        contribution.proposal_step,
        NotificationRuleTriggers.CONTRIBUTION_SUBMITTED,
        occurrence,
    )


# What the mail about a request for changes tells of each event; a lapse is
# no rule trigger, only mail to the step's actor.
CHANGES_LAPSED = "changes_lapsed"


def dispatch_changes_submitted(closed):
    """Queue the mail about one submission of the changes asked of the
    applicant, which closed every request in ``closed``.

    Each step's notification rules fire for its own request; whom the
    requests concern is told once (``tasks.notify_changes_submitted``),
    naming every request that concerns them.
    """
    from waldur_mastermind.proposal import tasks

    for change_request in closed:
        dispatch_step_event(
            change_request.step_instance,
            NotificationRuleTriggers.CHANGES_SUBMITTED,
            change_request.uuid.hex,
        )
    collector = dry_run_context.current()
    if collector is not None:
        collector.changes_submitted(closed)
        return
    uuids = [change_request.uuid.hex for change_request in closed]
    transaction.on_commit(lambda: tasks.notify_changes_submitted.delay(uuids))


def dispatch_change_request(change_request, trigger):
    """Queue the mail about a request for changes once the transaction commits.

    Whom it concerns is always told (``tasks.notify_change_request``): the
    applicant team of the request, the step's actor of the changes
    submitted or lapsed, the contributor of a reopened contribution. The
    step's notification rules for ``trigger`` add whoever else the call
    wants told. ``trigger`` None is a lapse.
    """
    from waldur_mastermind.proposal import tasks

    if trigger is not None:
        dispatch_step_event(
            change_request.step_instance, trigger, change_request.uuid.hex
        )
    change_request_uuid = change_request.uuid.hex
    event = trigger or CHANGES_LAPSED
    collector = dry_run_context.current()
    if collector is not None:
        collector.change_request(change_request, event)
        return
    transaction.on_commit(
        lambda: tasks.notify_change_request.delay(change_request_uuid, event)
    )


def dispatch_role_assigned(assignment):
    """Queue the mail telling the holder of ``assignment`` of their new
    evaluator role, once the transaction commits: the mail a role assigned
    by hand sends, for one the workflow assigned itself."""
    from waldur_mastermind.proposal import tasks

    assignment_uuid = assignment.uuid.hex
    transaction.on_commit(
        lambda: tasks.notify_proposal_role_changed.delay(assignment_uuid, True)
    )
