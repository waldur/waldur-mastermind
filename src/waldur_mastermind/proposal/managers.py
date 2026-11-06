from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db.models import (
    DateTimeField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
)
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from waldur_core.core.models import User
from waldur_core.core.utils import SubqueryCount
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_scope_ids, global_grant_sees_all
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.managers import MixinManager

from . import models, proposal_roles
from .enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    CallStates,
    ContributorSources,
    DelegationTargets,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    ReviewerStandings,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)


def round_open_q(prefix: str = "", now=None) -> Q:
    """Rounds open for submissions at ``now``, as a ``Q`` on ``prefix``.

    Started, and either before its cut-off or without one: a round with no
    cut-off stays open until a call manager closes it. A bare
    ``cutoff_time__gte`` would silently drop such a round, so every "open
    round" filter goes through here. Mirrors ``Round.status == OPEN``.

    ``prefix`` is the path to the round, e.g. ``"round"`` from a proposal or
    a call. The conditions form one ``Q``, so on a multi-valued path they
    apply to the same round when used in a single ``filter()``.
    """
    now = now or timezone.now()
    path = f"{prefix}__" if prefix else ""
    return Q(**{f"{path}start_time__lte": now}) & (
        Q(**{f"{path}cutoff_time__gte": now})
        | Q(**{f"{path}cutoff_time__isnull": True})
    )


def round_not_ended_q(prefix: str = "", now=None) -> Q:
    """Rounds not past their cut-off at ``now``, scheduled ones included.

    A round with no cut-off never ends by itself. The ``start_time`` term
    keeps a join on ``prefix`` from matching a call that has no round at all.
    """
    now = now or timezone.now()
    path = f"{prefix}__" if prefix else ""
    return Q(**{f"{path}start_time__isnull": False}) & (
        Q(**{f"{path}cutoff_time__gte": now})
        | Q(**{f"{path}cutoff_time__isnull": True})
    )


def round_overlaps(rounds, start_time, cutoff_time):
    """The ``rounds`` whose window overlaps ``[start_time, cutoff_time)``.

    A missing cut-off is +infinity on either side, so a round without one
    overlaps every round that starts after it starts.
    """
    rounds = rounds.filter(Q(cutoff_time__gt=start_time) | Q(cutoff_time__isnull=True))
    if cutoff_time is not None:
        rounds = rounds.filter(start_time__lt=cutoff_time)
    return rounds


class RoundQuerySet(django_models.QuerySet):
    def open_at(self, now=None):
        """Rounds open for submissions at ``now``; see ``round_open_q``."""
        return self.filter(round_open_q(now=now))


class CallQuerySet(django_models.QuerySet):
    def filter_for_user(self, user: User):
        if user.is_anonymous:
            return self.none()

        if user.is_staff or user.is_support:
            return self

        if global_grant_sees_all(user, PermissionEnum.LIST_CALLS):
            return self

        return self.filter(customer__in=get_connected_customers(user))


def sees_every_call(user) -> bool:
    """Whether a site-wide ``CALL.LIST`` grant should list every call-scoped row.

    List querysets that otherwise keep rows to call membership use this.
    Another call permission, such as ``ROUND.LIST``, does not. It does not
    make the user a call manager: ``get_connected_calls`` stays a membership
    lookup, and writes keep their own permission checks.
    """
    return global_grant_sees_all(user, PermissionEnum.LIST_CALLS)


class CallManager(MixinManager):
    def get_queryset(self):
        return CallQuerySet(self.model, using=self._db)


class ReviewQuerySet(django_models.QuerySet):
    def with_deadline(self):
        """Reviews that have a deadline, with it aliased as ``review_deadline``.

        Same rule as ``Review.review_end_date``: the deadline of the step the
        review contributes to, else created + the round's review duration,
        where an unset or zero duration means no deadline; pushed back to the
        assignment batch's deadline when the review was accepted from a batch
        that ends later. Evaluated in SQL so callers can filter and order on
        it.
        """
        step_deadline = F("contribution__proposal_step__deadline")
        round_deadline = ExpressionWrapper(
            F("created")
            + timedelta(days=1) * F("proposal__round__review_duration_in_days"),
            output_field=DateTimeField(),
        )
        own_deadline = Coalesce(
            step_deadline, round_deadline, output_field=DateTimeField()
        )
        return self.filter(
            Q(contribution__proposal_step__deadline__isnull=False)
            | Q(proposal__round__review_duration_in_days__gt=0)
        ).alias(
            review_deadline=Greatest(
                own_deadline,
                Coalesce(
                    F("assignment_item__batch__expires_at"),
                    own_deadline,
                    output_field=DateTimeField(),
                ),
                output_field=DateTimeField(),
            )
        )

    def due_within(self, days):
        """Reviews in progress whose deadline is at most ``days`` from now.

        Past deadlines are included: the hourly expiry task rejects those
        reviews, and until it runs they are the most urgent ones. The call
        manager dashboard count and the reviews list filter both use this.
        """
        return self.with_deadline().filter(
            state=models.Review.States.IN_REVIEW,
            review_deadline__lte=timezone.now() + timedelta(days=days),
        )


def _effective_status_q(
    field: str, values, waiting: str, expired: str, deadline: str
) -> Q:
    """Match rows on their effective status rather than the stored one.

    The periodic expiry tasks move a row from ``waiting`` to ``expired`` some
    time after its ``deadline`` passes. Until they run, the stored value is
    stale, so a row still ``waiting`` past its deadline is matched as
    ``expired`` and no longer as ``waiting``.
    """
    now = timezone.now()
    overdue = Q(**{field: waiting, f"{deadline}__lt": now})
    query = Q(pk__in=[])
    for value in set(values):
        if value == waiting:
            query |= Q(**{field: waiting}) & ~overdue
        elif value == expired:
            query |= Q(**{field: expired}) | overdue
        else:
            query |= Q(**{field: value})
    return query


def assignment_item_status_q(values, prefix: str = "") -> Q:
    """Assignment items whose effective status is one of ``values``.

    A pending item reads as expired once its batch's deadline has passed.
    """
    return _effective_status_q(
        f"{prefix}status",
        values,
        AssignmentItemStatuses.PENDING,
        AssignmentItemStatuses.EXPIRED,
        f"{prefix}batch__expires_at",
    )


def assignment_batch_status_q(values) -> Q:
    """Assignment batches whose effective status is one of ``values``.

    A sent batch reads as expired once its deadline has passed.
    """
    return _effective_status_q(
        "status",
        values,
        AssignmentBatchStatuses.SENT,
        AssignmentBatchStatuses.EXPIRED,
        "expires_at",
    )


def reviewer_pool_invitation_status_q(values) -> Q:
    """Reviewer pool entries whose effective invitation status is one of ``values``.

    A pending invitation reads as expired once its deadline has passed.
    """
    return _effective_status_q(
        "invitation_status",
        values,
        ReviewerPoolInvitationStatuses.PENDING,
        ReviewerPoolInvitationStatuses.EXPIRED,
        "invitation_expires_at",
    )


def open_assignment_items_q():
    """Assignment items that still occupy a reviewer's workload.

    A pending item counts unless its batch was cancelled or has expired,
    including a batch whose deadline has passed but which the expiry task has
    not marked yet; an accepted item counts until its review is submitted or
    rejected. Declined, expired, reassigned and COI-blocked items never count.
    """
    pending = assignment_item_status_q([AssignmentItemStatuses.PENDING]) & ~Q(
        batch__status__in=[
            AssignmentBatchStatuses.CANCELLED,
            AssignmentBatchStatuses.EXPIRED,
        ]
    )
    accepted = Q(status=AssignmentItemStatuses.ACCEPTED) & (
        Q(review__isnull=True) | Q(review__state=models.Review.States.IN_REVIEW)
    )
    return pending | accepted


class ReviewerProfileQuerySet(django_models.QuerySet):
    def eligible(self):
        """Profiles whose holders may be selected for new review work: in good
        standing with the operator and with an active account. Every path that
        picks reviewers from profiles starts here."""
        return self.filter(
            standing=ReviewerStandings.ACTIVE,
            user__is_active=True,
        )


class CallReviewerPoolQuerySet(django_models.QuerySet):
    def eligible(self):
        """Accepted pool members who may be given new review work: in good
        standing and with an active account. Every path that picks reviewers
        from a call's pool starts here."""
        return self.filter(
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            reviewer__standing=ReviewerStandings.ACTIVE,
            reviewer__user__is_active=True,
        )

    def with_open_assignments(self):
        """Annotate each pool entry with ``open_assignments``.

        The count is the entry's open assignment items (see
        ``open_assignment_items_q``) plus reviews in progress by the same
        reviewer on the call's proposals that were created directly rather
        than from an assignment item, so a review is never counted twice.
        """
        items = models.AssignmentItem.objects.filter(
            open_assignment_items_q(),
            batch__reviewer_pool_entry=OuterRef("pk"),
        ).values("pk")
        direct_reviews = models.Review.objects.filter(
            proposal__round__call=OuterRef("call_id"),
            reviewer=OuterRef("reviewer__user_id"),
            state=models.Review.States.IN_REVIEW,
            assignment_item__isnull=True,
        ).values("pk")
        return self.annotate(
            open_assignments=SubqueryCount(items) + SubqueryCount(direct_reviews)
        )


class RequestedOfferingQuerySet(django_models.QuerySet):
    def open_for_proposals(self):
        """Rows through which a proposal can be submitted right now.

        An accepted offering on an active call with a round that is **open**,
        and, when the call defines resource templates, one that covers the
        offering. Keep this the single definition — the ``open_for_proposals``
        field and filter and ``open_for_offering_uuid`` all build on it and must
        agree.

        Matches the write path exactly: ``ProposalSerializer.validate`` accepts
        a proposal only while its round is open, and ``ProposalViewSet.submit``
        applies the same rule. So nothing advertised here can be refused on
        creation, and nothing refused here would have been accepted.
        """
        # Subquery rather than a call__round join, which repeats the row per round.
        now = timezone.now()
        # Mirrors Round.status == OPEN: started, and not yet past its cutoff.
        call_has_open_round = models.Round.objects.open_at(now).filter(
            call=django_models.OuterRef("call"),
        )
        call_defines_templates = models.CallResourceTemplate.objects.filter(
            call=django_models.OuterRef("call")
        )
        # A template's call FK may differ from its requested offering's, so scope
        # coverage to the row's own call.
        covered_by_template = models.CallResourceTemplate.objects.filter(
            call=django_models.OuterRef("call"),
            requested_offering=django_models.OuterRef("pk"),
        )
        return (
            self.filter(
                django_models.Exists(call_has_open_round),
                state=RequestedOfferingStates.ACCEPTED,
                call__state=CallStates.ACTIVE,
            )
            # alias() rather than annotate(): these are filter-only, and annotate()
            # would push them into the SELECT list of every caller's values().
            .alias(
                _call_defines_templates=django_models.Exists(call_defines_templates),
                _covered_by_template=django_models.Exists(covered_by_template),
            )
            .filter(
                django_models.Q(_call_defines_templates=False)
                | django_models.Q(_covered_by_template=True)
            )
        )

    def offering_ids_open_for_proposals(self):
        return self.open_for_proposals().values_list("offering_id", flat=True)

    def offering_ids_in_active_calls(self):
        """Deprecated predicate behind the ``accessible_via_calls`` filter.

        Broader than ``open_for_proposals()``: it ignores rounds and resource
        template coverage, so it surfaces offerings no proposal can actually be
        submitted for. Kept so the published filter keeps its shipped meaning;
        drop it once no consumer remains.
        """
        return self.filter(
            state=RequestedOfferingStates.ACCEPTED,
            call__state=CallStates.ACTIVE,
        ).values_list("offering_id", flat=True)

    def call_ids_open_for_offering(self, offering_uuid):
        return (
            self.open_for_proposals()
            .filter(offering__uuid=offering_uuid)
            .values_list("call_id", flat=True)
        )


def annotate_offerings_open_for_proposals(queryset):
    """Resolve ``open_for_proposals`` for a marketplace Offering queryset.

    Every view serving offerings through ``PublicOfferingDetailsSerializer``
    should apply this; without it the serializer queries once per offering.
    """
    return queryset.annotate(
        open_for_proposals=django_models.Exists(
            models.RequestedOffering.objects.open_for_proposals().filter(
                offering=django_models.OuterRef("pk")
            )
        )
    )


def get_connected_call_organizers(user):
    ctype = ContentType.objects.get_for_model(models.CallManagingOrganisation)
    return get_scope_ids(user, ctype)


def get_connected_calls(user, role=None):
    ctype = ContentType.objects.get_for_model(models.Call)
    return get_scope_ids(user, ctype, role)


def get_results_readable_calls(user):
    """Calls whose published results ``user`` reads: those of organisations on
    which the user holds CALL.VIEW_RESULTS (an organisation reader, say)."""
    customer_ids = get_scope_ids(
        user,
        ContentType.objects.get_by_natural_key("structure", "customer"),
        permission=PermissionEnum.VIEW_CALL_RESULTS,
    )
    return models.Call.objects.filter(manager__customer_id__in=customer_ids).values(
        "id"
    )


def filter_reviews_for_user(user):
    """The reviews ``user`` may read.

    Staff, support and a site-wide CALL.LIST grant read every review. The call
    team -- organisers of the managing organisation and the call's managers --
    read the call's reviews, as do those deciding its proposals on the
    managing customer (CALL.APPROVE_AND_REJECT_PROPOSALS, its owners), and a
    reviewer their own. Any other role there reads none: an observer of the
    call's results or the organisation's support is no member of the call
    team, and reviewers' names and private comments are the call team's. The
    proposal's author reads its submitted reviews once the proposal is decided,
    if the call shows reviews to submitters.
    """
    if user.is_staff or user.is_support or sees_every_call(user):
        return models.Review.objects.all()

    team_proposal_ids = UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type__app_label="proposal",
        content_type__model="proposal",
    ).values("object_id")
    # The call team reads the reviews of the call's proposals but those it
    # applies with: on those, it is the applicant.
    authorized_query = (
        (
            Q(
                proposal__round__call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
            | Q(
                proposal__round__call__manager__customer_id__in=get_scope_ids(
                    user,
                    ContentType.objects.get_by_natural_key("structure", "customer"),
                    permission=PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
                )
            )
            | Q(proposal__round__call__in=get_connected_calls(user, CallRole.MANAGER))
        )
        & ~Q(proposal_id__in=team_proposal_ids)
    ) | Q(reviewer=user)
    submitter_query = Q(
        proposal_id__in=team_proposal_ids,
        proposal__round__call__reviews_visible_to_submitters=True,
        state=models.Review.States.SUBMITTED,
        proposal__state__in=[ProposalStates.ACCEPTED, ProposalStates.REJECTED],
    )
    return models.Review.objects.filter(authorized_query | submitter_query)


def never_submitted_q(prefix=""):
    """Proposals that never left draft: still in draft, or canceled from it
    when their round closed.

    A canceled proposal was submitted if it records when it left draft or
    walked any workflow step; proposals submitted before the submission time
    was recorded all have a step instance.
    """
    walked = models.ProposalWorkflowStepInstance.objects.filter(
        proposal_id=OuterRef(f"{prefix}pk")
    )
    return Q(**{f"{prefix}state": ProposalStates.DRAFT}) | (
        Q(**{f"{prefix}state": ProposalStates.CANCELED})
        & Q(**{f"{prefix}submitted_at__isnull": True})
        & ~Exists(walked)
    )


def _call_managed_allocated_proposals(user):
    """Accepted, allocated proposals of the calls ``user`` actively manages
    whose project is still where allocation created it: under the call's
    managing organisation. Moving the project to another organisation takes
    it out of the call's hands, and so out of the call manager's sight. A
    call manager on a proposal's applicant team reads its project as one of
    the team, never through the call."""
    return models.Proposal.objects.filter(
        state=ProposalStates.ACCEPTED,
        round__call__in=get_connected_calls(user, RoleEnum.CALL_MANAGER),
        project__isnull=False,
        project__customer=F("round__call__manager__customer"),
    ).exclude(pk__in=proposal_roles.applicant_team_proposal_ids(user))


def get_call_managed_project_ids(user):
    """Ids of the projects created for accepted proposals of the calls
    ``user`` manages.

    Registered with structure as a read-only project provider: a call manager
    follows up on what the call allocated without holding any role on the
    organisation that owns those projects. It opens the project record only;
    the resources in it are granted by ``get_call_allocated_resource_ids``.
    Lazy, so it folds into the caller's query; revoking the call role empties
    it.
    """
    return _call_managed_allocated_proposals(user).values_list("project_id", flat=True)


def get_call_allocated_resource_ids(user):
    """Ids of the resources allocation provisioned for those proposals.

    Allocation records the resource it creates for each awarded item on the
    item (and on the request it was prefilled from; proposals allocated before
    awards existed have only the latter). Neither link is writable through the
    API, so this is exactly what the call allocated: a resource ordered into
    the project by other means is not among them, nor is an allocated resource
    once it has been moved out of the proposal's project.
    """
    proposals = _call_managed_allocated_proposals(user)
    awarded = models.AwardedResource.objects.filter(
        proposal__in=proposals,
        resource__project=F("proposal__project"),
    ).values("resource_id")
    requested = models.RequestedResource.objects.filter(
        proposal__in=proposals,
        resource__project=F("proposal__project"),
    ).values("resource_id")
    return marketplace_models.Resource.objects.filter(
        Q(id__in=awarded) | Q(id__in=requested)
    ).values_list("id", flat=True)


def get_offering_manager_proposals(user):
    """Proposal ids an offering manager (technical reviewer) may read.

    Offering managers hold OFFERING.MANAGER on the Offering, not on the Call, so
    they are invisible to ``get_connected_calls``. They are the responsible role
    for the ``technical_assessment`` step, so they need read access — but only to
    the **submitted** proposals that actually **requested one of their accepted
    offerings**, not to every (possibly unsubmitted, cross-provider) proposal on
    the call -- or that were **awarded** one, when the call manager moved an item
    onto an offering nobody requested: its provider provisions the award, so
    reads it once the award is released (not while a draft or held). A
    draft canceled when its round closed was never submitted, so it stays the
    applicant's.
    """
    offering_ctype = ContentType.objects.get_for_model(marketplace_models.Offering)
    offering_ids = get_scope_ids(user, offering_ctype, RoleEnum.OFFERING_MANAGER)
    requested = (
        models.RequestedResource.objects.filter(
            requested_offering__offering_id__in=offering_ids,
            requested_offering__state=RequestedOfferingStates.ACCEPTED,
        )
        .exclude(never_submitted_q("proposal__"))
        .order_by()
        .values_list("proposal_id", flat=True)
    )
    # An award decides nothing for the offering it names until it is
    # released: a draft can still change, and a decision held for the round
    # is announced to no one yet.
    released = Q(proposal__state=ProposalStates.ACCEPTED) | (
        Q(proposal__decision_held=False)
        & Exists(
            models.ProposalWorkflowStepInstance.objects.filter(
                proposal_id=OuterRef("proposal_id"),
                is_allocation_decision=True,
                status=WorkflowStepInstanceStatuses.COMPLETED,
                outcome=WorkflowStepOutcomes.APPROVED,
            )
        )
    )
    awarded = (
        models.AwardedResource.objects.filter(
            released,
            requested_offering__offering_id__in=offering_ids,
            requested_offering__state=RequestedOfferingStates.ACCEPTED,
        )
        .order_by()
        .values_list("proposal_id", flat=True)
    )
    return requested.union(awarded)


def get_live_reviews(user):
    """The user's reviews that are in review or submitted.

    A rejected review is one that was cancelled or expired, so it no longer
    gives the reviewer a reason to read the proposal. Nor does a review of a
    proposal whose applicant team the reviewer is on.
    """
    return (
        models.Review.objects.filter(reviewer=user)
        .exclude(state=models.Review.States.REJECTED)
        .exclude(
            Exists(
                UserRole.objects.filter(
                    content_type__app_label="proposal",
                    content_type__model="proposal",
                    object_id=OuterRef("proposal_id"),
                    user_id=OuterRef("reviewer_id"),
                    is_active=True,
                )
            )
        )
    )


def get_reviewed_proposals(user):
    """Proposal ids the user holds a live review for."""
    return get_live_reviews(user).values_list("proposal_id", flat=True)


def holds_live_review(user, proposal) -> bool:
    return get_live_reviews(user).filter(proposal=proposal).exists()


def get_assigned_proposals(user):
    """Proposal ids offered to the user in a sent assignment batch and not
    yet answered: they read the proposal to decide whether to take it.

    An accepted item reads on through the review it created, and stops with
    it once that review is rejected; a declined, expired, reassigned or
    blocked item grants nothing.
    """
    return (
        models.AssignmentItem.objects.filter(
            batch__reviewer_pool_entry__reviewer__user=user,
            batch__status__in=AssignmentBatchStatuses.SENT_TO_REVIEWER,
            status=AssignmentItemStatuses.PENDING,
        )
        .order_by()
        .values_list("proposal_id", flat=True)
    )


def _given_by_call_role(responsible_role):
    """Contributions a holder of the call role ``responsible_role`` makes as
    such: to a step of the role's holders, or the completer's form of a step
    without contributors the role completes."""
    step = "proposal_step__call_step__"
    return Q(
        **{
            f"{step}contributor_source": f"{ContributorSources.CALL_ROLE}:"
            f"{responsible_role}"
        }
    ) | Q(
        **{
            f"{step}contributor_source__in": ("", ContributorSources.NONE),
            f"{step}completer": responsible_role,
        }
    )


def _without_lapsed_call_roles(contributions, user):
    """``contributions`` but those a call role gave ``user`` that they no
    longer hold on the contribution's call: revoked, or lapsed and swept."""
    call_type = ContentType.objects.get_for_model(models.Call)
    for responsible_role, role_name in proposal_roles.CONTRIBUTOR_CALL_ROLES.items():
        alias = f"holds_{responsible_role}"
        contributions = contributions.alias(
            **{
                alias: Exists(
                    UserRole.objects.filter(
                        user=user,
                        is_active=True,
                        content_type=call_type,
                        object_id=OuterRef("proposal_step__proposal__round__call_id"),
                    ).filter(
                        Q(role__name=role_name) | Q(role__template__name=role_name)
                    )
                )
            }
        ).exclude(_given_by_call_role(responsible_role) & Q(**{alias: False}))
    # The panel chair and vice-chair are seats on the call, not roles: they
    # hold one while the call names them (it stops naming them once their
    # panel member role is revoked).
    contributions = contributions.alias(
        chairs_panel=Exists(
            models.Call.objects.filter(
                pk=OuterRef("proposal_step__proposal__round__call_id")
            ).filter(Q(panel_chair=user) | Q(panel_vice_chair=user))
        )
    ).exclude(_given_by_call_role(ResponsibleRoles.PANEL_CHAIR) & Q(chairs_panel=False))
    return contributions


def get_contributed_proposals(user, per_offering=None):
    """Proposal ids the user holds a contribution to a step of that still
    counts: pending, accepted or submitted. ``per_offering`` narrows them to
    the slots of a requested offering (True) or to the others (False).

    Not one to a step of an evaluator role's holders: those read the
    proposal while they hold the role (``proposal_roles.proposals_held_by``),
    and stop once it is revoked. Nor one a call role gave them -- to a step
    of the role's holders, or the form of a step the role completes -- once
    they no longer hold it on the call. Nor one to a proposal a conflict of
    interest of theirs with stands.
    """
    contributed = (
        models.StepContribution.objects.filter(contributor=user)
        .exclude(status__in=StepContributionStatuses.CLOSED)
        .exclude(
            proposal_step__call_step__contributor_source__startswith=(
                f"{ContributorSources.PROPOSAL_ROLE}:"
            )
        )
    )
    if per_offering is not None:
        contributed = contributed.filter(requested_offering__isnull=not per_offering)
    return (
        _without_lapsed_call_roles(contributed, user)
        .exclude(
            Exists(
                models.ConflictOfInterest.objects.filter(
                    reviewer__user=user,
                    proposal_id=OuterRef("proposal_step__proposal_id"),
                    status__in=proposal_roles.BLOCKING_COI_STATUSES,
                )
            )
        )
        .order_by()
        .values_list("proposal_step__proposal_id", flat=True)
    )


def get_delegated_proposals(user):
    """Proposal ids a per-offering slot handed to the user opens to them:
    once they accepted it (or wrote it), and only while they may still take
    it over -- a role on the slot's own offering held, not on the applicant
    team, no conflict of interest standing. Nothing before they accept it,
    and nothing of the call's other proposals.

    The holders of an evaluator role a person's slot is handed to read the
    proposal through that role already.
    """
    offering_type = ContentType.objects.get_for_model(marketplace_models.Offering)
    return (
        models.StepContribution.objects.filter(
            delegate=user,
            status__in=(
                StepContributionStatuses.ACCEPTED,
                StepContributionStatuses.SUBMITTED,
            ),
            requested_offering__isnull=False,
            proposal_step__call_step__delegable_to=DelegationTargets.OFFERING_TEAM,
        )
        .filter(
            Exists(
                UserRole.objects.filter(
                    user=user,
                    is_active=True,
                    content_type=offering_type,
                    object_id=OuterRef("requested_offering__offering_id"),
                )
            )
        )
        .exclude(
            proposal_step__proposal_id__in=proposal_roles.applicant_team_proposal_ids(
                user
            )
        )
        .exclude(
            Exists(
                models.ConflictOfInterest.objects.filter(
                    reviewer__user=user,
                    proposal_id=OuterRef("proposal_step__proposal_id"),
                    status__in=proposal_roles.BLOCKING_COI_STATUSES,
                )
            )
        )
        .order_by()
        .values_list("proposal_step__proposal_id", flat=True)
    )


def holds_reviewer_work(user, proposal) -> bool:
    """Whether a reviewer holds work on ``proposal``: a live review, a live
    assignment item, a contribution that still counts or an evaluator role.

    The call's reviewer role is a pool, not a grant on the call's proposals;
    a reviewer reads, and acts on, only the proposals they hold work on.
    """
    if user is None or user.is_anonymous:
        return False
    return (
        holds_live_review(user, proposal)
        or get_assigned_proposals(user).filter(proposal_id=proposal.pk).exists()
        or get_contributed_proposals(user)
        .filter(proposal_step__proposal_id=proposal.pk)
        .exists()
        or proposal_roles.holds_role(user, proposal)
    )
