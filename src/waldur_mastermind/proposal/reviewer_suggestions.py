"""Reviewers suggested for a proposal by its evaluators.

A step that ``collects_reviewer_suggestions`` is an assessment step whose
contributors hold an evaluator role on the proposal (its rapporteurs, say).
While it is active each holder suggests reviewers through their own
contribution: an existing user or an e-mail address, with a note on their
expertise. Holders are resolved by the evaluator-role resolver, so nobody
whose assignment was revoked, or against whom a conflict of interest
stands, may suggest.

The call's managers see a proposal's suggestions merged per suggested person,
with who suggested them and the conflicts of interest known for that person,
and turn them into invitations to the call's reviewer pool -- through the
same path as an invitation by e-mail, so the call's response window and the
invitation e-mail apply. A suggestion then follows its invitation. Once a
suggested reviewer accepted, the call's managers assign them to the
proposal's review step like any other pool member.

Suggestions are the evaluators' work: nothing here is shown to the
applicant team, and nobody on it may suggest, see or act on them.
"""

from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import exceptions

from waldur_core.configuration import config
from waldur_core.core.models import User
from waldur_mastermind.proposal import (
    contributions,
    models,
    proposal_roles,
    reviewer_pool,
    reviewer_standing,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    COIStatuses,
    ProposalReviewerSuggestionStatuses,
    ResponsibleRoles,
    ReviewerInvitationOutcomes,
    ReviewerPoolInvitationStatuses,
    ReviewerStandings,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
)

Statuses = ProposalReviewerSuggestionStatuses

# Conflicts of interest shown against a suggested reviewer: all but those
# found not to be one.
FLAGGED_COI_STATUSES = (COIStatuses.PENDING, COIStatuses.RECUSED, COIStatuses.WAIVED)


# =============================================================================
# Who may do what
# =============================================================================


def suggesting_step(proposal):
    """The proposal's active step that collects reviewer suggestions, if one
    does; of several steps running at once, the first such."""
    return (
        proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE,
            call_step__collects_reviewer_suggestions=True,
        )
        .select_related("call_step")
        .order_by("order", "id")
        .first()
    )


def collecting_steps(proposal):
    return proposal.workflow_step_instances.filter(
        call_step__collects_reviewer_suggestions=True
    ).select_related("call_step")


def may_view(user, proposal) -> bool:
    """Whether ``user`` sees all of the proposal's suggestions.

    Staff and support, those overseeing the call, and the holders of the
    evaluator role completing a step that collects them -- resolved by the
    evaluator-role resolver, so a revoked holder or one against whom a
    conflict stands does not read. Never anyone on the applicant team, staff
    and support included; nor anyone but staff with a conflict of interest
    recorded on the proposal.
    """
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if user.is_staff:
        return True
    if proposal_roles.recorded_conflicts(user, proposal).exists():
        return False
    if user.is_support:
        return True
    if proposal_permissions.oversees_proposal_call(user, proposal):
        return True
    return any(
        instance.call_step.completer == ResponsibleRoles.PROPOSAL_ROLE
        and instance.call_step.completer_proposal_role_id is not None
        and proposal_roles.holds_role(
            user, proposal, instance.call_step.completer_proposal_role_id
        )
        for instance in collecting_steps(proposal)
    )


def reads_addresses(user, proposal) -> bool:
    """Whether ``user``, reading the proposal's suggestions, reads the
    suggested people's addresses: staff, support and those overseeing the
    call, who manage its reviewer pool. The completer of the step collecting
    suggestions reads what the evaluators typed, not the address of a user
    they picked, nor which user a typed address belongs to."""
    return user.is_support or may_invite(user, proposal)


def may_invite(user, proposal) -> bool:
    """Whether ``user`` turns suggestions into pool invitations: staff and
    those overseeing the call, who manage its reviewer pool. Never anyone on
    the applicant team, staff included."""
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if user.is_staff:
        return True
    return proposal_permissions.oversees_proposal_call(user, proposal)


# =============================================================================
# Suggesting
# =============================================================================


def suggestion_contribution(user, proposal):
    """The contribution ``user`` suggests reviewers through, or an error.

    Their own open contribution to the proposal's active step collecting
    suggestions, while they hold the step's evaluator role.
    """
    instance = suggesting_step(proposal)
    if instance is None:
        raise exceptions.ValidationError(
            "Reviewers are suggested while a step collecting suggestions is active."
        )
    if proposal_roles.is_on_applicant_team(user, proposal) or not (
        contributions.may_contribute(user, instance)
    ):
        raise exceptions.PermissionDenied(
            "Only the holders of the step's evaluator role suggest reviewers."
        )
    try:
        contribution = contributions.resolve_contribution(user, instance)
    except contributions.ContributionError as exc:
        raise exceptions.ValidationError(str(exc))
    if contribution is None or contribution.contributor_id != user.id:
        raise exceptions.PermissionDenied(
            "Only the holders of the step's evaluator role suggest reviewers."
        )
    if contribution.status not in StepContributionStatuses.OPEN:
        raise exceptions.ValidationError(
            f"Your contribution is {contribution.status}; no more suggestions."
        )
    return contribution


def may_suggest(user, instance) -> bool:
    """Whether ``user`` may suggest a reviewer through ``instance`` now: the
    proposal's active step collecting suggestions, while their own
    contribution to it is open. The same rule ``suggest`` enforces, asked
    without raising, and without opening a slot as suggesting would, so the
    page offers only what it would accept."""
    proposal = instance.proposal
    if suggesting_step(proposal) != instance:
        return False
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if not contributions.may_contribute(user, instance):
        return False
    try:
        contribution = contributions.resolve_contribution(user, instance, create=False)
    except contributions.ContributionError:
        return False
    if contribution is None:
        # Suggesting opens the caller's own slot where the step makes them
        # on demand.
        return contributions._may_open_own_slot(user, instance)
    return (
        contribution.contributor_id == user.id
        and contribution.status in StepContributionStatuses.OPEN
    )


def suggest(user, proposal, suggested_user=None, suggested_email="", note=""):
    """Record ``user``'s suggestion of a reviewer for ``proposal``."""
    suggested_email = (suggested_email or "").strip().lower()
    if bool(suggested_user) == bool(suggested_email):
        raise exceptions.ValidationError("Suggest either a user or an e-mail address.")
    contribution = suggestion_contribution(user, proposal)
    # A typed address is taken as typed: whose it is, if anyone's, is for
    # the call's managers to find out (see ``candidates_of``), who never
    # invite someone on the applicant team.
    if suggested_user is not None:
        if suggested_user.pk == user.pk:
            raise exceptions.ValidationError("You cannot suggest yourself.")
        if proposal_roles.is_on_applicant_team(suggested_user, proposal):
            raise exceptions.ValidationError(
                "A member of the applicant team cannot review the proposal."
            )
    elif user.email and suggested_email == user.email.lower():
        raise exceptions.ValidationError("You cannot suggest yourself.")
    own = models.ProposalReviewerSuggestion.objects.filter(contribution=contribution)
    duplicate = (
        own.filter(suggested_user=suggested_user)
        if suggested_user
        else own.filter(suggested_user__isnull=True, suggested_email=suggested_email)
    )
    if duplicate.exists():
        raise exceptions.ValidationError("You have already suggested this reviewer.")
    try:
        with transaction.atomic():
            suggestion = models.ProposalReviewerSuggestion.objects.create(
                proposal=proposal,
                contribution=contribution,
                suggested_user=suggested_user,
                suggested_email="" if suggested_user else suggested_email,
                note=note or "",
            )
            _follow_invitation(proposal, suggestion)
    except IntegrityError:
        raise exceptions.ValidationError("You have already suggested this reviewer.")
    return suggestion


def _follow_invitation(proposal, suggestion):
    """Link a new suggestion of a person already invited from the proposal's
    suggestions to that invitation, so it stands where theirs do."""
    rows = list(_suggestions_of(proposal, lock=True))
    for candidate in _group(proposal, rows):
        if any(row.pk == suggestion.pk for row in candidate.suggestions):
            invited = next(
                (row for row in candidate.suggestions if row.pool_entry_id), None
            )
            if invited is not None:
                suggestion.pool_entry = invited.pool_entry
                suggestion.invited_by = invited.invited_by
                suggestion.invited_at = invited.invited_at
                suggestion.save(
                    update_fields=["pool_entry", "invited_by", "invited_at", "modified"]
                )
            return


def withdraw(user, suggestion):
    """Take back one's own suggestion while it is still pending and the step
    collecting it active."""
    contribution = suggestion.contribution
    if contribution.contributor_id != user.id:
        raise exceptions.PermissionDenied("Only who suggested it takes it back.")
    if suggestion.current_status != Statuses.PENDING:
        raise exceptions.ValidationError(
            "The suggestion has been acted on and cannot be taken back."
        )
    instance = suggesting_step(suggestion.proposal)
    if instance is None or instance.pk != contribution.proposal_step_id:
        raise exceptions.ValidationError("The step collecting suggestions is over.")
    # Who suggested it may have lost the role since, or a conflict may stand.
    if not contributions.may_contribute(user, instance):
        raise exceptions.PermissionDenied(
            "Only the holders of the step's evaluator role take suggestions back."
        )
    suggestion.delete()


# =============================================================================
# Finding someone to suggest
# =============================================================================

# Shorter queries match too much of the user base to be a search.
CANDIDATE_QUERY_MIN_LENGTH = 3
CANDIDATES_LIMIT = 20


def may_search_candidates(user, proposal) -> bool:
    """Whether ``user`` searches for reviewers to suggest: whoever may
    suggest, a holder of the evaluator role contributing to the active step
    collecting suggestions. Nobody on the applicant team."""
    instance = suggesting_step(proposal)
    if instance is None:
        return False
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    return contributions.may_contribute(user, instance)


@dataclass
class SuggestionCandidate:
    """A registered user the caller may suggest, without their address."""

    user: User
    affiliation: str | None
    has_published_profile: bool
    pool_status: str | None
    has_conflict: bool

    @property
    def uuid(self):
        return self.user.uuid

    @property
    def full_name(self):
        return self.user.full_name


def _current_affiliation(user):
    profile = getattr(user, "reviewer_profile", None)
    if profile is None:
        return None
    affiliations = profile.current_affiliations
    if not affiliations:
        return None
    affiliation = affiliations[0]
    if affiliation.organization is not None:
        return affiliation.organization.name
    return affiliation.organization_name or None


def _matching(users, search):
    """``users`` matching every word of ``search`` on name, username or
    e-mail, case-insensitively."""
    for term in search.split():
        users = users.filter(
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(username__icontains=term)
            | Q(email__icontains=term)
        )
    return users


def suggestion_candidates(user, proposal, search):
    """Registered users ``user`` may suggest as reviewers of ``proposal``.

    Those with a published reviewer profile and those already in the call's
    reviewer pool, matching ``search``, at most ``CANDIDATES_LIMIT``. Left
    out: the applicant team, the caller and whoever holds an evaluator role
    on the proposal. A conflict of interest standing between a person and
    the proposal is flagged, not hidden.
    """
    search = (search or "").strip()
    if len(search) < CANDIDATE_QUERY_MIN_LENGTH:
        return []
    call = proposal.round.call
    pool = models.CallReviewerPool.objects.filter(call=call)
    # Published profiles are discoverable only while the operator keeps
    # reviewer profiles on; the call's pool always is.
    published = (
        Q(reviewer_profile__is_published=True)
        if config.REVIEWER_PROFILES_ENABLED
        else Q(pk__in=[])
    )
    users = User.objects.filter(is_active=True).filter(
        published
        | Q(id__in=pool.filter(reviewer__isnull=False).values("reviewer__user_id"))
        | Q(id__in=pool.filter(invited_user__isnull=False).values("invited_user_id"))
    )
    users = (
        _matching(users, search)
        # Whoever the operator has suspended or blocked is not suggested.
        .exclude(
            reviewer_profile__standing__in=[
                ReviewerStandings.SUSPENDED,
                ReviewerStandings.BLOCKED,
            ]
        )
        .exclude(pk=user.pk)
        .exclude(id__in=proposal_roles.applicant_team_user_ids(proposal))
        .exclude(id__in=proposal_roles.active_assignments(proposal).values("user_id"))
        .select_related("reviewer_profile")
        .prefetch_related(
            Prefetch(
                "reviewer_profile__affiliations",
                queryset=models.ReviewerAffiliation.objects.filter(
                    end_date__isnull=True
                )
                .select_related("organization")
                .order_by("-is_primary", "-start_date", "id"),
                to_attr="current_affiliations",
            )
        )
        .order_by("first_name", "last_name", "username")
    )
    # The call's reviewer eligibility, when it sets one, admits the rest.
    admits = reviewer_standing.admission_check(call)
    found = [person for person in users[:CANDIDATES_LIMIT] if admits(person)]
    if not found:
        return []
    found_ids = {person.pk for person in found}
    conflicted = set(
        models.ConflictOfInterest.objects.filter(
            proposal=proposal,
            reviewer__user_id__in=found_ids,
            status__in=proposal_roles.BLOCKING_COI_STATUSES,
        ).values_list("reviewer__user_id", flat=True)
    )
    entries = {}
    for entry in (
        pool.filter(
            Q(reviewer__user_id__in=found_ids) | Q(invited_user_id__in=found_ids)
        )
        .select_related("reviewer")
        .order_by("pk")
    ):
        for person_id in (
            entry.reviewer.user_id if entry.reviewer is not None else None,
            entry.invited_user_id,
        ):
            if person_id is not None:
                entries.setdefault(person_id, entry)
    result = []
    for person in found:
        profile = getattr(person, "reviewer_profile", None)
        entry = entries.get(person.pk)
        result.append(
            SuggestionCandidate(
                user=person,
                affiliation=_current_affiliation(person),
                has_published_profile=bool(profile and profile.is_published),
                pool_status=(
                    models.ProposalReviewerSuggestion.pool_entry_status(entry)
                    if entry is not None
                    else None
                ),
                has_conflict=person.pk in conflicted,
            )
        )
    return result


# =============================================================================
# The call manager's view: suggestions merged per suggested person
# =============================================================================


def applicant_excluded_user_ids(proposal) -> set[int]:
    """Users the applicant asked not to review the proposal.

    Applicants cannot name excluded reviewers yet; once they can, their
    choices are flagged on the suggestions from here.
    """
    return set()


@dataclass
class Candidate:
    """One suggested person and every suggestion of them for the proposal."""

    key: str
    user: User | None
    email: str
    suggestions: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    is_applicant_excluded: bool = False
    holds_evaluator_role: bool = False
    is_on_applicant_team: bool = False
    already_in_pool: bool = False
    # The person's entry in the call's reviewer pool, however they got there.
    pool_match: models.CallReviewerPool | None = None
    # Where the latest assignment of the person to the proposal stands: a
    # draft batch still has to be sent before the reviewer hears of it.
    assignment_batch_status: str | None = None

    @property
    def pool_entry(self):
        for suggestion in self.suggestions:
            if suggestion.pool_entry is not None:
                return suggestion.pool_entry
        return None

    @property
    def status(self) -> str:
        if self.pool_entry is not None:
            return models.ProposalReviewerSuggestion.pool_entry_status(self.pool_entry)
        if all(s.discarded_at is not None for s in self.suggestions):
            return Statuses.DISCARDED
        return Statuses.PENDING

    @property
    def has_conflict(self) -> bool:
        return any(
            c.status in proposal_roles.BLOCKING_COI_STATUSES for c in self.conflicts
        )

    @property
    def invitation_email(self) -> str:
        if self.user is not None:
            return self.user.email or ""
        return self.email


def _suggestions_of(proposal, uuids=None, lock=False):
    rows = models.ProposalReviewerSuggestion.objects.filter(proposal=proposal)
    if uuids is not None:
        rows = rows.filter(uuid__in=uuids)
    if lock:
        rows = rows.select_for_update(of=("self",))
    return rows.select_related(
        "suggested_user",
        "contribution__contributor",
        "contribution__proposal_step__call_step",
        "pool_entry",
    ).order_by("created", "id")


def _group(proposal, rows, resolve_emails=True):
    """``rows`` merged per suggested person: a typed address of a user is
    theirs, unless ``resolve_emails`` is off, when it stands for itself."""
    emails = {r.suggested_email for r in rows if not r.suggested_user_id}
    users_by_email = {}
    if emails and resolve_emails:
        query = Q()
        for email in emails:
            query |= Q(email__iexact=email)
        for user in User.objects.filter(query, is_active=True):
            users_by_email.setdefault(user.email.lower(), user)
    groups = {}
    for row in rows:
        user = row.suggested_user or users_by_email.get(row.suggested_email)
        if user is not None:
            key = f"user:{user.uuid.hex}"
        else:
            key = f"email:{row.suggested_email}"
        candidate = groups.get(key)
        if candidate is None:
            candidate = groups[key] = Candidate(
                key=key,
                user=user,
                email=user.email if user is not None else row.suggested_email,
            )
        candidate.suggestions.append(row)
    return list(groups.values())


def _flag(proposal, candidates, resolve_emails=True):
    users = [c.user for c in candidates if c.user is not None]
    user_ids = {u.pk for u in users}
    conflicts = {}
    for coi in models.ConflictOfInterest.objects.filter(
        proposal=proposal,
        reviewer__user_id__in=user_ids,
        status__in=FLAGGED_COI_STATUSES,
    ).select_related("reviewer"):
        conflicts.setdefault(coi.reviewer.user_id, []).append(coi)
    excluded = applicant_excluded_user_ids(proposal)
    holders = set(
        proposal_roles.proposal_role_holders(proposal).values_list("id", flat=True)
    )
    team = proposal_roles.applicant_team_user_ids(proposal)
    pool = reviewer_pool.PoolIndex(
        proposal.round.call, users, [c.invitation_email for c in candidates]
    )
    for candidate in candidates:
        user = candidate.user
        if user is not None:
            candidate.conflicts = conflicts.get(user.pk, [])
            candidate.is_applicant_excluded = user.pk in excluded
            candidate.holds_evaluator_role = user.pk in holders
            candidate.is_on_applicant_team = user.pk in team
        if user is not None or resolve_emails:
            candidate.pool_match = pool.find(user, candidate.invitation_email)
        candidate.already_in_pool = (
            candidate.pool_entry is not None or candidate.pool_match is not None
        )
    _flag_assignments(proposal, candidates)
    return candidates


def _flag_assignments(proposal, candidates):
    entries = {}
    for candidate in candidates:
        entry = candidate.pool_entry or candidate.pool_match
        if entry is not None:
            entries.setdefault(entry.pk, []).append(candidate)
    if not entries:
        return
    latest = (
        models.AssignmentItem.objects.filter(
            proposal=proposal, batch__reviewer_pool_entry_id__in=entries
        )
        .exclude(batch__status=AssignmentBatchStatuses.CANCELLED)
        .order_by("batch__reviewer_pool_entry_id", "-created", "-id")
        .values_list("batch__reviewer_pool_entry_id", "batch__status")
    )
    seen = set()
    for entry_id, batch_status in latest:
        if entry_id in seen:
            continue
        seen.add(entry_id)
        for candidate in entries[entry_id]:
            candidate.assignment_batch_status = batch_status


def candidates_of(proposal, resolve_emails=True):
    """The proposal's suggestions merged per suggested person, flagged.

    Without ``resolve_emails`` a typed address is never matched to a user,
    nor to the call's reviewer pool, for a reader not to learn whose it is
    (see ``reads_addresses``).
    """
    rows = list(_suggestions_of(proposal))
    return _flag(
        proposal,
        _group(proposal, rows, resolve_emails=resolve_emails),
        resolve_emails=resolve_emails,
    )


# =============================================================================
# Acting on suggestions
# =============================================================================


def _named_candidates(proposal, suggestion_uuids, lock):
    named = list(_suggestions_of(proposal, suggestion_uuids))
    if len(named) != len(set(map(str, suggestion_uuids))):
        raise exceptions.ValidationError(
            {"suggestion_uuids": "Some suggestions are not this proposal's."}
        )
    # Acting on a person acts on every suggestion of them for the proposal.
    rows = list(_suggestions_of(proposal, lock=lock))
    keys = {c.key for c in _group(proposal, named)}
    return [c for c in _group(proposal, rows) if c.key in keys]


InvitationOutcomes = ReviewerInvitationOutcomes


@dataclass
class InvitationResult:
    candidate: Candidate
    outcome: str
    detail: str = ""

    @property
    def invited(self) -> bool:
        """Whether an invitation e-mail goes out."""
        return self.outcome in (InvitationOutcomes.INVITED, InvitationOutcomes.RESENT)


def _declined_detail(entry) -> str:
    when = entry.response_date or entry.modified
    return (
        f"Declined the invitation on {timezone.localdate(when).isoformat()}; "
        "not re-invited."
    )


def _link(candidate, entry, invited_by, now, every_row=False):
    """Point the candidate's suggestions at ``entry``: the rows not linked
    yet, or ``every_row`` when the invitation was (re)sent now. Returns how
    many rows changed."""
    rows = [
        row
        for row in candidate.suggestions
        if every_row or row.pool_entry_id != entry.pk
    ]
    for row in rows:
        row.pool_entry = entry
        row.invited_by = invited_by
        row.invited_at = now
        row.discarded_at = None
        row.discarded_by = None
        row.modified = now
    if rows:
        models.ProposalReviewerSuggestion.objects.bulk_update(
            rows,
            [
                "pool_entry",
                "invited_by",
                "invited_at",
                "discarded_at",
                "discarded_by",
                "modified",
            ],
        )
    return len(rows)


def _invite_one(call, candidate, invited_by, now):
    Outcomes = InvitationOutcomes
    linked = candidate.pool_entry
    entry = linked or candidate.pool_match
    if linked is None and candidate.is_on_applicant_team:
        return InvitationResult(
            candidate,
            Outcomes.REFUSED,
            "A member of the applicant team cannot review the proposal.",
        )
    if entry is not None:
        status = models.ProposalReviewerSuggestion.pool_entry_status(entry)
        if status == Statuses.DECLINED:
            if linked is not None:
                # Every suggestion of them stands where the invitation does.
                _link(candidate, entry, invited_by, now)
            return InvitationResult(
                candidate, Outcomes.REFUSED, _declined_detail(entry)
            )
        if status == Statuses.EXPIRED:
            try:
                entry = reviewer_pool.resend_invitation(entry)
            except exceptions.ValidationError as exc:
                return InvitationResult(
                    candidate, Outcomes.REFUSED, " ".join(str(d) for d in exc.detail)
                )
            _link(candidate, entry, invited_by, now, every_row=True)
            return InvitationResult(
                candidate,
                Outcomes.RESENT,
                "The invitation had expired; sent again.",
            )
        changed = _link(candidate, entry, invited_by, now)
        if linked is None:
            return InvitationResult(
                candidate,
                Outcomes.LINKED,
                "Already in the call's reviewer pool; linked.",
            )
        if changed:
            return InvitationResult(
                candidate, Outcomes.LINKED, "Already invited; linked."
            )
        return InvitationResult(
            candidate,
            Outcomes.REFUSED,
            "Already accepted the invitation."
            if status == Statuses.ACCEPTED
            else "Already invited.",
        )
    email = candidate.invitation_email
    if not email:
        return InvitationResult(
            candidate, Outcomes.REFUSED, "The user has no e-mail address."
        )
    try:
        entry = reviewer_pool.invite_by_email(call, email, invited_by)
    except exceptions.ValidationError as exc:
        # Not in good standing, or outside the call's reviewer eligibility.
        return InvitationResult(
            candidate, Outcomes.REFUSED, " ".join(str(d) for d in exc.detail)
        )
    except reviewer_pool.AlreadyInPool as exc:
        # Invited meanwhile, under another address of theirs or concurrently.
        if exc.entry.invitation_status == ReviewerPoolInvitationStatuses.DECLINED:
            return InvitationResult(
                candidate, Outcomes.REFUSED, _declined_detail(exc.entry)
            )
        _link(candidate, exc.entry, invited_by, now)
        return InvitationResult(
            candidate,
            Outcomes.LINKED,
            "Already in the call's reviewer pool; linked.",
        )
    _link(candidate, entry, invited_by, now, every_row=True)
    return InvitationResult(candidate, Outcomes.INVITED)


def invite(proposal, suggestion_uuids, invited_by):
    """Invite the people suggested in ``suggestion_uuids`` to the call's
    reviewer pool, and link every suggestion of them to the invitation.

    A person already in the pool is linked to their entry, not invited
    again; an expired invitation is sent again, and one declined is left
    alone. Nobody on the applicant team is invited.
    """
    call = proposal.round.call
    with transaction.atomic():
        candidates = _flag(
            proposal, _named_candidates(proposal, suggestion_uuids, True)
        )
        now = timezone.now()
        return [
            _invite_one(call, candidate, invited_by, now) for candidate in candidates
        ]


def discard(proposal, suggestion_uuids, user):
    """Set aside the people suggested in ``suggestion_uuids``, unless invited."""
    with transaction.atomic():
        candidates = _named_candidates(proposal, suggestion_uuids, True)
        now = timezone.now()
        for candidate in candidates:
            if candidate.pool_entry is not None:
                raise exceptions.ValidationError(
                    f"{candidate.email or candidate.key} has been invited already."
                )
            for suggestion in candidate.suggestions:
                suggestion.discarded_at = now
                suggestion.discarded_by = user
                suggestion.modified = now
            models.ProposalReviewerSuggestion.objects.bulk_update(
                candidate.suggestions, ["discarded_at", "discarded_by", "modified"]
            )
    return candidates


# =============================================================================
# Permission checks for the proposal's actions
# =============================================================================


def can_view(request, view, obj=None):
    if obj is None:
        return
    if not may_view(request.user, obj):
        raise exceptions.PermissionDenied(
            "You cannot see the reviewers suggested for this proposal."
        )


can_view.sources = ["*"]


def can_invite(request, view, obj=None):
    if obj is None:
        return
    if not may_invite(request.user, obj):
        raise exceptions.PermissionDenied(
            "Only the call's managers invite suggested reviewers."
        )


can_invite.sources = ["*"]


def can_suggest(request, view, obj=None):
    """The applicant team is refused outright; whether the caller holds the
    step's evaluator role is checked when suggesting."""
    if obj is None:
        return
    if proposal_roles.is_on_applicant_team(request.user, obj) and not (
        request.user.is_staff
    ):
        raise exceptions.PermissionDenied(
            "Only the holders of the step's evaluator role suggest reviewers."
        )


can_suggest.sources = ["*"]


def can_search_candidates(request, view, obj=None):
    if obj is None:
        return
    if not may_search_candidates(request.user, obj):
        raise exceptions.PermissionDenied(
            "Only the holders of the step's evaluator role suggest reviewers."
        )


can_search_candidates.sources = ["*"]
