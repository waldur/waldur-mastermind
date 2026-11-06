"""What the provider of an offering sees of the calls it is in.

A call's workflow can hand work to the team of each offering the call
accepted: a step the offering managers complete, or one each requested
offering's team contributes to (a technical assessment per partition, say).
The call's managers cannot invite those people; the offering's provider
manages its team in the marketplace. These helpers tell the provider which
steps its team acts in and whether anyone is on the team to act, without
opening the rest of the call's configuration to it.

Only the OFFERING.MANAGER role counts as the offering's team here, as it
does for steps, contributions and notifications
(``permissions.offering_manager_user_ids_for_proposal``): the owner of the
provider organisation answers requests but does not assess.
"""

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import RoleEnum
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    DelegationTargets,
    NotificationRuleRecipients,
    ProposalStates,
    ResponsibleRoles,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
)


class TeamParts:
    """How an offering's team takes part in a step."""

    COMPLETES = "completes"
    CONTRIBUTES = "contributes"

    CHOICES = (
        (COMPLETES, "The offering managers complete the step"),
        (CONTRIBUTES, "Each requested offering's team contributes"),
    )


# Proposals a provider is shown: an applicant's draft is not an application yet.
SHOWN_PROPOSAL_EXCLUDED_STATES = (ProposalStates.DRAFT,)


def team_steps_q(prefix=""):
    """Enabled workflow steps the offering team completes or contributes to."""
    return Q(**{f"{prefix}is_enabled": True}) & (
        Q(**{f"{prefix}completer": ResponsibleRoles.OFFERING_MANAGER})
        | Q(
            **{
                f"{prefix}contributor_source": ContributorSources.REQUESTED_OFFERING_TEAM
            }
        )
    )


def team_part(step) -> str | None:
    """The team's part in ``step``, or None when it has none.

    A step the team both contributes to and completes is the team's to
    complete."""
    if step.completer == ResponsibleRoles.OFFERING_MANAGER:
        return TeamParts.COMPLETES
    if step.contributor_source == ContributorSources.REQUESTED_OFFERING_TEAM:
        return TeamParts.CONTRIBUTES
    return None


def team_steps(call):
    """The enabled steps of ``call`` the offering team acts in, in order."""
    return list(
        call.workflow_steps.filter(team_steps_q())
        .prefetch_related("notification_rules")
        .order_by("order", "id")
    )


def team_notification_triggers(step) -> list[str]:
    """Triggers of the step's enabled rules that mail the offering team."""
    triggers = []
    for rule in step.notification_rules.all():
        if not rule.is_enabled:
            continue
        reaches_team = rule.recipient == NotificationRuleRecipients.OFFERING_TEAMS or (
            rule.recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE
            and step.completer == ResponsibleRoles.OFFERING_MANAGER
        )
        if reaches_team and rule.trigger not in triggers:
            triggers.append(rule.trigger)
    return triggers


def delegable_within_team(step) -> bool:
    return step.delegable_to == DelegationTargets.OFFERING_TEAM


def _offering_manager_roles():
    # A deactivated user staffs nothing: their slots go to the stand-in.
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        user__is_active=True,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
    )


def offering_manager_counts(offering_ids) -> dict[int, int]:
    """Offering id -> how many people hold OFFERING.MANAGER on it."""
    counts = {offering_id: 0 for offering_id in offering_ids}
    for row in (
        _offering_manager_roles()
        .filter(object_id__in=list(counts))
        .values("object_id")
        .annotate(count=Count("user_id", distinct=True))
    ):
        counts[row["object_id"]] = row["count"]
    return counts


def _subquery_count(queryset, field, distinct=True):
    """``COUNT(field)`` of a correlated queryset, 0 when it has no rows."""
    return Coalesce(
        Subquery(
            queryset.order_by()
            .annotate(_group=Value(1))
            .values("_group")
            .annotate(count=Count(field, distinct=distinct))
            .values("count")[:1],
            output_field=IntegerField(),
        ),
        0,
    )


def annotate_requested_offerings(queryset):
    """Annotate requested offerings with what the provider's list shows:
    proposals requesting each, the offering's managers, and the steps of the
    call the offering's team acts in."""
    proposals = models.RequestedResource.objects.filter(
        requested_offering_id=OuterRef("pk")
    ).exclude(proposal__state__in=SHOWN_PROPOSAL_EXCLUDED_STATES)
    managers = _offering_manager_roles().filter(object_id=OuterRef("offering_id"))
    steps = models.CallWorkflowStep.objects.filter(
        team_steps_q(), call_id=OuterRef("call_id")
    )
    return queryset.annotate(
        proposals_count=_subquery_count(proposals, "proposal_id"),
        offering_managers_count=_subquery_count(managers, "user_id"),
        team_steps_count=_subquery_count(steps, "pk", distinct=False),
    )


def current_round(rounds):
    """The round of a call that matters now: the open one, else the next to
    open, else the one that ended last. None for a call without rounds."""
    rounds = list(rounds)
    if not rounds:
        return None
    now = timezone.now()
    open_rounds = [
        r
        for r in rounds
        if r.start_time <= now and (r.cutoff_time is None or r.cutoff_time >= now)
    ]
    if open_rounds:
        return min(open_rounds, key=lambda r: r.start_time)
    upcoming = [r for r in rounds if r.start_time > now]
    if upcoming:
        return min(upcoming, key=lambda r: r.start_time)
    return max(rounds, key=lambda r: (r.cutoff_time or r.start_time, r.pk))


def _person(role) -> dict:
    user = role.user
    return {
        "uuid": user.uuid,
        "full_name": user.full_name or user.username,
        "email": user.email or "",
        "role_name": role.role.name,
        "role_description": role.role.description or "",
    }


def team_people(offering) -> dict:
    """The offering's team by name: its managers, who do the team's part of
    each step, and everyone else holding a role on it, whom a slot may be
    handed to. Active users only; one row per person, their manager role
    first."""
    roles = (
        permissions_models.UserRole.objects.filter(
            is_active=True,
            user__is_active=True,
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
            object_id=offering.pk,
        )
        .select_related("user", "role")
        .order_by("user__first_name", "user__last_name", "user__username")
    )
    managers, delegates, seen = [], [], set()
    for role in sorted(roles, key=lambda r: r.role.name != RoleEnum.OFFERING_MANAGER):
        if role.user_id in seen:
            continue
        seen.add(role.user_id)
        (managers if role.role.name == RoleEnum.OFFERING_MANAGER else delegates).append(
            _person(role)
        )
    return {"managers": managers, "delegates": delegates}


def open_team_slots(requested_offering) -> list[dict]:
    """The offering team's open slots in the call: one per proposal at a
    step where the team has a part, with who holds it."""
    now = timezone.now()
    staffed = bool(
        offering_manager_counts([requested_offering.offering_id]).get(
            requested_offering.offering_id
        )
    )
    slots = (
        models.StepContribution.objects.filter(
            requested_offering=requested_offering,
            status__in=StepContributionStatuses.OPEN,
            proposal_step__status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        .select_related(
            "proposal_step__proposal", "proposal_step__call_step", "delegate"
        )
        .order_by("due_at", "created")
    )
    return [
        {
            "uuid": slot.uuid,
            "proposal_uuid": slot.proposal_step.proposal.uuid,
            "proposal_name": slot.proposal_step.proposal.name,
            "proposal_slug": slot.proposal_step.proposal.slug or "",
            "call_name": requested_offering.call.name,
            "offering_name": requested_offering.offering.name,
            "requested_offering_uuid": requested_offering.uuid,
            "step_name": slot.proposal_step.call_step.name,
            "step_key": slot.proposal_step.call_step.key,
            "status": slot.status,
            "delegate_uuid": slot.delegate.uuid if slot.delegate_id else None,
            "delegate_name": (
                slot.delegate.full_name or slot.delegate.username
                if slot.delegate_id
                else None
            ),
            "delegate_accepted": bool(
                slot.delegate_id and slot.status == StepContributionStatuses.ACCEPTED
            ),
            "stand_in": not staffed,
            "due_at": slot.due_at,
            "overdue": bool(slot.due_at and slot.due_at < now),
        }
        for slot in slots
    ]
