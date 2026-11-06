import django_filters
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.core.enums import ReviewStates as CoreReviewStates
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.proposal.enums import (
    CallStates,
    PostAwardStatuses,
    ProposalRunStates,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    ReviewerStandings,
    StepKinds,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)

from . import models, proposal_roles, serializers
from . import permissions as proposal_permissions
from .managers import (
    assignment_batch_status_q,
    assignment_item_status_q,
    get_assigned_proposals,
    get_connected_call_organizers,
    get_connected_calls,
    get_contributed_proposals,
    get_offering_manager_proposals,
    get_reviewed_proposals,
    reviewer_pool_invitation_status_q,
    round_not_ended_q,
)


class CallResourceTemplateFilter(django_filters.FilterSet):
    call = core_filters.URLFilter(
        view_name="proposal-protected-call-detail",
        field_name="call__uuid",
        label="Call",
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    requested_offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="requested_offering__uuid",
    )
    name = django_filters.CharFilter(lookup_expr="icontains")
    is_required = django_filters.BooleanFilter()
    o = django_filters.OrderingFilter(fields=("created", "name", "is_required"))

    class Meta:
        model = models.CallResourceTemplate
        fields = []


class CallManagingOrganisationFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_keyword = django_filters.CharFilter(method="filter_customer_keyword")
    o = django_filters.OrderingFilter(fields=(("customer__name", "customer_name"),))

    class Meta:
        model = models.CallManagingOrganisation
        fields = []

    def filter_customer_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(customer__name__icontains=value)
            | Q(customer__abbreviation__icontains=value)
            | Q(customer__native_name__icontains=value)
        )


class CallFilter(django_filters.FilterSet):
    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="manager__customer__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="manager__customer__uuid"
    )
    customer_keyword = django_filters.CharFilter(method="filter_customer_keyword")
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", method="filter_offering_uuid"
    )
    open_for_offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        method="filter_open_for_offering_uuid",
        label="Calls the offering can be requested through right now",
    )
    state = django_filters.MultipleChoiceFilter(choices=CallStates.CHOICES)
    o = django_filters.OrderingFilter(
        fields=("manager__customer__name", "created", "name")
    )
    has_active_round = django_filters.BooleanFilter(
        widget=BooleanWidget, method="filter_has_active_round"
    )
    name = django_filters.CharFilter(lookup_expr="icontains")
    offerings_provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="offerings__customer__uuid"
    )

    class Meta:
        model = models.Call
        fields = []

    def filter_customer_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(manager__customer__name__icontains=value)
            | Q(manager__customer__abbreviation__icontains=value)
            | Q(manager__customer__native_name__icontains=value)
        )

    def filter_has_active_round(self, queryset, name, value):
        if value:
            # An archived call is listed for its history, but its rounds take
            # no proposals, whatever their dates say.
            return (
                queryset.exclude(state=CallStates.ARCHIVED)
                .filter(round_not_ended_q("round"))
                .distinct()
            )
        return queryset

    def filter_offering_uuid(self, queryset, name, value):
        return queryset.filter(offerings__uuid=value).distinct()

    def filter_open_for_offering_uuid(self, queryset, name, value):
        """Calls a proposal for this offering can be submitted to right now.

        Narrower than ``offering_uuid``, which matches every call the offering was
        ever added to. Shares the predicate behind ``open_for_proposals``.
        """
        return queryset.filter(
            id__in=models.RequestedOffering.objects.call_ids_open_for_offering(value)
        )


class PublicCallFilter(CallFilter):
    """The public list holds active and archived calls only: a draft is its
    managers' to see, on their own list, so there is no draft to filter."""

    state = django_filters.MultipleChoiceFilter(
        choices=[
            (value, label)
            for value, label in CallStates.CHOICES
            if value in (CallStates.ACTIVE, CallStates.ARCHIVED)
        ]
    )


class ProposalFilter(django_filters.FilterSet):
    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )
    round = core_filters.RelatedUUIDFilter(
        view_name="call-round-detail", field_name="round__uuid"
    )
    round_uuid = core_filters.RelatedUUIDFilter(
        view_name="call-round-detail", field_name="round__uuid"
    )
    state = django_filters.MultipleChoiceFilter(choices=ProposalStates.CHOICES)
    # The key of a step of the proposal's call: each call names its own.
    workflow_step = core_filters.CharInFilter(
        field_name="current_step__step", label="Current workflow step"
    )
    name = django_filters.CharFilter(lookup_expr="icontains")
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="round__call__uuid"
    )
    organization_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="round__call__manager__customer__uuid"
    )
    created_by_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", method="filter_created_by_uuid"
    )
    my_proposals = django_filters.BooleanFilter(
        method="filter_my_proposals",
        widget=BooleanWidget,
    )
    involved = django_filters.BooleanFilter(
        method="filter_involved",
        widget=BooleanWidget,
        label="Only proposals the current user takes part in, leaving out "
        "those read only through staff or support access",
    )
    panel_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-protected-call-panels",
        method="filter_panel_uuid",
        label="Panel UUID",
    )
    changes_requested = django_filters.BooleanFilter(
        method="filter_changes_requested",
        widget=BooleanWidget,
        label="Only proposals waiting on changes asked of their applicant",
    )
    as_role = django_filters.ChoiceFilter(
        choices=ResponsibleRoles.CHOICES,
        method="filter_as_role",
        label="Only proposals the current user holds this role on",
    )
    post_award_status = django_filters.MultipleChoiceFilter(
        choices=PostAwardStatuses.CHOICES,
        method="filter_post_award_status",
        label="Where an accepted proposal's extension or final report stands",
    )
    run_phase = django_filters.ChoiceFilter(
        choices=[
            choice
            for choice in WorkflowPhases.CHOICES
            if choice[0] in WorkflowPhases.POST_AWARD
        ],
        method="filter_run_phase",
        label="Only proposals with a run of this phase",
    )
    run_state = django_filters.ChoiceFilter(
        choices=ProposalRunStates.CHOICES,
        method="filter_run_state",
        label="Only proposals with a run in this state",
    )
    final_report_overdue = django_filters.BooleanFilter(
        method="filter_final_report_overdue",
        widget=BooleanWidget,
        label="Only proposals whose final report is past its due date",
    )
    o = django_filters.OrderingFilter(
        fields=(
            "round__call__name",
            "round__start_time",
            "round__cutoff_time",
            "state",
            "created",
            "slug",
        )
    )

    def filter_created_by_uuid(self, queryset, name, value):
        """Proposals created by the given user, among those whose author the
        viewer may know.

        An evaluator from whom the call conceals the applicant's username does
        not get those proposals back: a match would confirm who wrote them.
        They are left out rather than the request refused, because a refusal
        that depended on the matches would confirm the same thing.
        """
        queryset = queryset.filter(created_by__uuid=value)
        visibility = serializers.ApplicantVisibility(self.request.user)
        attributed = [
            proposal.pk
            for proposal in queryset.select_related("round__call")
            if "username" not in (visibility.concealed_attributes(proposal) or ())
        ]
        return queryset.filter(pk__in=attributed)

    def _run_readers_only(self, queryset, matching):
        """``matching`` narrowed to the proposals whose runs the user reads:
        a filter on runs must not tell a reviewer what followed an award
        (``permissions.user_can_view_runs``)."""
        user = self.request.user
        ids = queryset.filter(matching).values_list("pk", flat=True).distinct()
        if not (user.is_staff or user.is_support):
            ids = [
                proposal.pk
                for proposal in models.Proposal.objects.filter(
                    pk__in=list(ids)
                ).select_related("round__call")
                if proposal_permissions.user_can_view_runs(user, proposal)
            ]
        return queryset.filter(pk__in=list(ids))

    @staticmethod
    def _post_award_q(value):
        open_run = Exists(
            models.ProposalRun.objects.filter(
                proposal=OuterRef("pk"), state=ProposalRunStates.OPEN
            )
        )

        def open_of(phase):
            return Exists(
                models.ProposalRun.objects.filter(
                    proposal=OuterRef("pk"),
                    state=ProposalRunStates.OPEN,
                    phase=phase,
                )
            )

        at_applicant = Q(current_step__call_step__kind=StepKinds.APPLICANT_ACTION)
        accepted = Q(state=ProposalStates.ACCEPTED, completed_at__isnull=True)
        extension, closing = WorkflowPhases.EXTENSION, WorkflowPhases.CLOSING
        return {
            PostAwardStatuses.PROJECT_COMPLETED: Q(
                state=ProposalStates.ACCEPTED, completed_at__isnull=False
            ),
            PostAwardStatuses.PENDING_FINAL_REPORT: accepted
            & Q(open_of(closing))
            & at_applicant,
            PostAwardStatuses.FINAL_REPORT_SUBMITTED: accepted
            & Q(open_of(closing))
            & ~at_applicant,
            PostAwardStatuses.EXTENSION_REQUESTED: accepted
            & Q(open_of(extension))
            & at_applicant,
            PostAwardStatuses.EXTENSION_IN_ASSESSMENT: accepted
            & Q(open_of(extension))
            & Q(
                current_step__call_step__kind__in=(
                    StepKinds.ASSESSMENT,
                    StepKinds.ASSIGNMENT,
                )
            ),
            PostAwardStatuses.EXTENSION_PENDING_RESULTS: accepted
            & Q(open_of(extension))
            & Q(current_step__call_step__kind=StepKinds.DECISION),
            PostAwardStatuses.PROJECT_EXTENDED: accepted
            & ~Q(open_run)
            & Q(
                Exists(
                    models.ProposalRun.objects.filter(
                        proposal=OuterRef("pk"),
                        phase=extension,
                        state=ProposalRunStates.APPROVED,
                    )
                )
            ),
        }[value]

    def filter_post_award_status(self, queryset, name, value):
        if not value:
            return queryset
        matching = Q()
        for status in value:
            matching |= self._post_award_q(status)
        return self._run_readers_only(queryset, matching)

    def filter_run_phase(self, queryset, name, value):
        return self._run_readers_only(queryset, Q(runs__phase=value))

    def filter_run_state(self, queryset, name, value):
        return self._run_readers_only(queryset, Q(runs__state=value))

    def filter_final_report_overdue(self, queryset, name, value):
        overdue = Q(
            workflow_step_instances__run__phase=WorkflowPhases.CLOSING,
            workflow_step_instances__run__state=ProposalRunStates.OPEN,
            workflow_step_instances__call_step__kind=StepKinds.APPLICANT_ACTION,
            workflow_step_instances__status=WorkflowStepInstanceStatuses.ACTIVE,
            workflow_step_instances__deadline__lt=timezone.now(),
        )
        matching = self._run_readers_only(queryset, overdue)
        if value:
            return matching
        return queryset.exclude(pk__in=matching.values("pk"))

    def filter_changes_requested(self, queryset, name, value):
        waiting = Q(
            state=ProposalStates.IN_REVIEW,
            change_requests__closed_at__isnull=True,
            change_requests__contribution__isnull=True,
            change_requests__step_instance__status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        ids = queryset.filter(waiting).values("pk")
        if value:
            return queryset.filter(pk__in=ids)
        return queryset.exclude(pk__in=ids)

    def filter_my_proposals(self, queryset, name, value):
        """Filter to show only proposals created by the current user."""
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        return queryset.filter(created_by=user)

    def filter_involved(self, queryset, name, value):
        """The reader's own proposals, as anyone but staff and support reads
        them: a staff member's "My proposals" lists what they take part in,
        not the whole site (``permissions.involved_proposals``)."""
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        return queryset.filter(
            id__in=proposal_permissions.involved_proposals(user).values("id")
        )

    def filter_panel_uuid(self, queryset, name, value):
        """Proposals routed to the panel. Never the reader's own team's
        proposals: the applicant team is not told its proposal's panel, so a
        filter must not tell it either."""
        queryset = queryset.filter(panel__uuid=value)
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        if user.is_staff or user.is_support:
            return queryset
        return queryset.exclude(id__in=proposal_roles.applicant_team_proposal_ids(user))

    def filter_as_role(self, queryset, name, value):
        """Narrow the readable queryset to what one of the reader's roles reaches.

        In a peer-review deployment one person is routinely an applicant on one
        call, a reviewer on another, a call manager on a third and an offering
        manager for a provider. ``filter_proposals`` grants the *union* of all
        of that, so an unfiltered list mixes their own applications with every
        proposal of the calls they run. This intersects that union with a single
        role's own scope, so the reader can ask for one hat at a time.

        It only ever narrows. Every branch is a subset of what
        ``filter_proposals`` already granted, and an unrecognised role is
        rejected by the ChoiceFilter before it gets here — so this grants
        nothing that the plain list would have withheld, and callers still get
        today's permission checks on the objects themselves.
        """
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()

        team = proposal_roles.applicant_team_proposal_ids(user)
        if value == ResponsibleRoles.APPLICANT:
            # The proposals whose team the user is on: a member added to
            # someone else's proposal (PROPOSAL.MEMBER / PROPOSAL.MANAGER) is
            # an applicant on it too, which ``my_proposals`` -- created_by
            # only -- misses, and an author removed from the team is not.
            return queryset.filter(id__in=team)

        if value == ResponsibleRoles.CALL_MANAGER:
            # Both ways a call is managed, as CALL_PERMISSION_SOURCES has it: a
            # CALL.MANAGER on the call itself, or a CUSTOMER.CALL_ORGANIZER on
            # the organisation running it. No distinct() needed for the reverse
            # join: filter_queryset_for_user already ends with one.
            return queryset.filter(
                Q(round__call__in=get_connected_calls(user, RoleEnum.CALL_MANAGER))
                | Q(
                    round__call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                        user
                    )
                )
            )

        if value == ResponsibleRoles.OFFERING_MANAGER:
            # Offering managers hold no call-scoped role at all, so the call
            # traversals above cannot see them; this is the same predicate that
            # granted them read access in the first place.
            # Nobody evaluates a proposal whose team they are on.
            return queryset.filter(id__in=get_offering_manager_proposals(user)).exclude(
                id__in=team
            )

        if value == ResponsibleRoles.PROPOSAL_ROLE:
            # An evaluator role (a rapporteur, say) is held per proposal, not
            # through a call role; the predicate that granted the access.
            return queryset.filter(id__in=proposal_roles.proposals_held_by(user))

        if value == ResponsibleRoles.PANEL_CHAIR:
            # The submitted proposals of the calls whose panel the user
            # chairs or vice-chairs; a panel member's reach, so a subset.
            return (
                queryset.filter(
                    Q(round__call__panel_chair=user)
                    | Q(round__call__panel_vice_chair=user)
                )
                .filter(
                    round__call__in=get_connected_calls(
                        user, RoleEnum.CALL_PANEL_MEMBER
                    )
                )
                .exclude(state=models.Proposal.States.DRAFT)
                .exclude(id__in=team)
            )

        # Fails closed on a role with no call-role mapping: a new call-level role
        # added to ResponsibleRoles (the observer of waldur-mastermind#546 would
        # be one) becomes a valid choice the moment it is declared, and showing
        # it the whole readable union is the one outcome this filter prevents.
        call_role = {
            ResponsibleRoles.REVIEWER: RoleEnum.CALL_REVIEWER,
            ResponsibleRoles.PANEL_MEMBER: RoleEnum.CALL_PANEL_MEMBER,
        }.get(value)
        if not call_role:
            return queryset.none()

        # A reviewer: the proposals they hold reviewer work on -- a live
        # review, a sent assignment they have not declined, a contribution
        # that still counts -- as models.filter_proposals grants them. The
        # call's reviewer role is the pool and grants no proposal by itself.
        # A panel member: the call's submitted proposals, as the committee.
        if value == ResponsibleRoles.REVIEWER:
            by_role = (
                Q(pk__in=get_reviewed_proposals(user))
                | Q(pk__in=get_assigned_proposals(user))
                | Q(pk__in=get_contributed_proposals(user))
            )
        else:
            by_role = Q(round__call__in=get_connected_calls(user, call_role)) & ~Q(
                state=models.Proposal.States.DRAFT
            )
        return queryset.filter(by_role).exclude(id__in=team)

    class Meta:
        model = models.Proposal
        fields = []


class ProposalRoleAssignmentFilter(django_filters.FilterSet):
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-protected-call-detail",
        field_name="proposal__round__call__uuid",
    )
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid"
    )
    is_active = django_filters.BooleanFilter(
        field_name="revoked_at", lookup_expr="isnull", widget=BooleanWidget
    )

    class Meta:
        model = models.ProposalRoleAssignment
        fields = []


class ReviewFilter(django_filters.FilterSet):
    proposal = core_filters.URLFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    proposal_name = django_filters.CharFilter(
        field_name="proposal__name", lookup_expr="icontains"
    )
    organization_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="proposal__round__call__manager__customer__uuid",
    )
    o = django_filters.OrderingFilter(fields=("created", "state"))
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail",
        field_name="proposal__round__call__uuid",
    )
    round_uuid = core_filters.RelatedUUIDFilter(
        view_name="call-round-detail", field_name="proposal__round__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail", field_name="reviewer__uuid"
    )
    state = django_filters.MultipleChoiceFilter(choices=models.Review.States.CHOICES)
    due_within_days = django_filters.NumberFilter(
        method="filter_due_within_days",
        min_value=0,
        label="Due within days",
        help_text=(
            "Only reviews in progress whose deadline is at most this many days "
            "away, including ones already past it. Implies state=in_review."
        ),
    )
    managed_calls_only = django_filters.BooleanFilter(
        method="filter_managed_calls_only",
        widget=BooleanWidget,
        help_text=(
            "Only reviews on calls where the current user is call manager, "
            "the same scope as the call manager dashboard."
        ),
    )

    class Meta:
        model = models.Review
        fields = []

    def filter_due_within_days(self, queryset, name, value):
        # Also narrows to in-review reviews, whatever the state filter says.
        # NumberFilter yields a Decimal, which timedelta does not accept.
        return queryset.due_within(int(value))

    def filter_managed_calls_only(self, queryset, name, value):
        if not value:
            return queryset
        managed_call_ids = get_connected_calls(self.request.user, CallRole.MANAGER)
        return queryset.filter(proposal__round__call_id__in=managed_call_ids)


class RequestedOfferingFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="offering__customer__uuid",
        label="Provider",
    )
    organization_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="call__manager__customer__uuid"
    )
    call = core_filters.URLFilter(
        view_name="proposal-public-call-detail",
        field_name="call__uuid",
        label="Call",
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    call_state = django_filters.MultipleChoiceFilter(
        field_name="call__state", choices=CallStates.CHOICES, label="Call state"
    )
    o = django_filters.OrderingFilter(
        fields=("created", "state", "offering__name", "call__name")
    )
    state = django_filters.MultipleChoiceFilter(choices=RequestedOfferingStates.CHOICES)

    class Meta:
        model = models.RequestedOffering
        fields = []


class RequestedResourceFilter(django_filters.FilterSet):
    involved = django_filters.BooleanFilter(
        method="filter_involved",
        widget=BooleanWidget,
        label="Only requests of proposals the current user takes part in, "
        "leaving out those read only through staff or support access",
    )
    # RequestedResource reaches the offering through RequestedOffering; it has no
    # offering FK of its own, so the shorter path raises FieldError at query time.
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="requested_offering__offering__uuid",
        label="Offering",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="requested_offering__offering__uuid",
    )
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    proposal = core_filters.URLFilter(
        view_name="proposal-proposal-detail",
        field_name="proposal__uuid",
        label="Proposal",
    )
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="requested_offering__offering__customer__uuid",
        label="Provider",
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail",
        field_name="requested_offering__call__uuid",
    )
    proposal_state = django_filters.MultipleChoiceFilter(
        field_name="proposal__state",
        choices=ProposalStates.CHOICES,
        label="Proposal state",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by offering, proposal, call or resource name",
    )
    o = django_filters.OrderingFilter(
        fields=(
            ("created", "created"),
            ("requested_offering__offering__name", "offering__name"),
            ("resource__name", "resource__name"),
            ("resource__state", "resource__state"),
            ("proposal__name", "proposal__name"),
            ("proposal__state", "proposal__state"),
            ("proposal__round__call__name", "call__name"),
        )
    )

    def filter_involved(self, queryset, name, value):
        """As ``ProposalFilter.filter_involved``, so "My proposals" and its
        resource lens list the same proposals."""
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        return proposal_permissions.involved_requested_resources(user, queryset)

    def filter_query(self, queryset, name, value):
        # The four names a row actually shows, so the box matches what is read.
        return queryset.filter(
            Q(requested_offering__offering__name__icontains=value)
            | Q(proposal__name__icontains=value)
            | Q(proposal__round__call__name__icontains=value)
            | Q(resource__name__icontains=value)
        )

    class Meta:
        model = models.RequestedResource
        fields = ["created"]


class CallWorkflowStepNotificationRuleFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail",
        field_name="workflow_step__call__uuid",
    )
    workflow_step_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-call-workflow_step-detail",
        field_name="workflow_step__uuid",
    )
    step = django_filters.CharFilter(field_name="workflow_step__key")
    trigger = django_filters.CharFilter()
    is_enabled = django_filters.BooleanFilter()

    class Meta:
        model = models.CallWorkflowStepNotificationRule
        fields = []


class ProposalProjectRoleMappingFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )

    class Meta:
        model = models.ProposalProjectRoleMapping
        fields = []


# =============================================================================
# Reviewer Profile Filters
# =============================================================================


class ExpertiseCategoryFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    code = django_filters.CharFilter(lookup_expr="icontains")
    parent_uuid = core_filters.RelatedUUIDFilter(
        view_name="expertise-category-detail", field_name="parent__uuid"
    )
    level = django_filters.NumberFilter()
    o = django_filters.OrderingFilter(fields=("code", "name", "level"))

    class Meta:
        model = models.ExpertiseCategory
        fields = []


class ReviewerRegistryFilter(django_filters.FilterSet):
    query = django_filters.CharFilter(method="filter_query")
    standing = django_filters.MultipleChoiceFilter(choices=ReviewerStandings.CHOICES)
    publication_pending = django_filters.BooleanFilter(
        method="filter_publication_pending", widget=BooleanWidget
    )
    o = django_filters.OrderingFilter(
        fields=(
            ("user__first_name", "user_name"),
            ("user__email", "user_email"),
            ("standing_changed_at", "standing_changed_at"),
            "created",
        )
    )

    class Meta:
        model = models.ReviewerProfile
        fields = []

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
            | Q(user__email__icontains=value)
            | Q(user__username__icontains=value)
        )

    def filter_publication_pending(self, queryset, name, value):
        pending = Q(publication_requests__state=CoreReviewStates.PENDING)
        return (
            queryset.filter(pending).distinct() if value else queryset.exclude(pending)
        )


class ReviewerProfileFilter(django_filters.FilterSet):
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid"
    )
    orcid_id = django_filters.CharFilter(lookup_expr="iexact")
    user_email = django_filters.CharFilter(
        field_name="user__email", lookup_expr="icontains"
    )
    user_name = django_filters.CharFilter(method="filter_user_name")
    has_orcid = django_filters.BooleanFilter(
        method="filter_has_orcid", widget=BooleanWidget
    )
    expertise_keyword = django_filters.CharFilter(
        field_name="expertise_set__expertise_keyword", lookup_expr="icontains"
    )
    expertise_category_uuid = core_filters.RelatedUUIDFilter(
        view_name="expertise-category-detail",
        field_name="expertise_set__expertise_category__uuid",
    )
    o = django_filters.OrderingFilter(
        fields=(
            ("user__full_name", "user_name"),
            ("user__email", "user_email"),
            "created",
        )
    )

    class Meta:
        model = models.ReviewerProfile
        fields = []

    def filter_user_name(self, queryset, name, value):
        return queryset.filter(
            Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
            | Q(user__full_name__icontains=value)
            | Q(alternative_names__icontains=value)
        )

    def filter_has_orcid(self, queryset, name, value):
        if value:
            return queryset.exclude(orcid_id="").exclude(orcid_id__isnull=True)
        return queryset.filter(Q(orcid_id="") | Q(orcid_id__isnull=True))


# =============================================================================
# COI Filters
# =============================================================================


class ConflictOfInterestFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    round_uuid = core_filters.RelatedUUIDFilter(
        view_name="call-round-detail", field_name="proposal__round__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail", field_name="reviewer__uuid"
    )
    reviewer_name = django_filters.CharFilter(method="filter_reviewer_name")
    coi_type = django_filters.MultipleChoiceFilter(
        choices=models.ConflictOfInterest._meta.get_field("coi_type").choices
    )
    severity = django_filters.ChoiceFilter(
        choices=models.ConflictOfInterest._meta.get_field("severity").choices
    )
    status = django_filters.MultipleChoiceFilter(
        choices=models.ConflictOfInterest._meta.get_field("status").choices
    )
    detection_method = django_filters.MultipleChoiceFilter(
        choices=models.ConflictOfInterest._meta.get_field("detection_method").choices
    )
    o = django_filters.OrderingFilter(
        fields=("detected_at", "severity", "status", "created")
    )

    class Meta:
        model = models.ConflictOfInterest
        fields = []

    def filter_reviewer_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, "reviewer__user")


class COIDisclosureFormFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail", field_name="reviewer__uuid"
    )
    is_current = django_filters.BooleanFilter(widget=BooleanWidget)
    certified = django_filters.BooleanFilter(widget=BooleanWidget)
    o = django_filters.OrderingFilter(
        fields=("created", "certification_date", "valid_until")
    )

    class Meta:
        model = models.COIDisclosureForm
        fields = []


class CallReviewerPoolFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail", field_name="reviewer__uuid"
    )
    invitation_status = django_filters.MultipleChoiceFilter(
        choices=models.CallReviewerPool._meta.get_field("invitation_status").choices,
        method="filter_invitation_status",
    )
    my_invitations = django_filters.BooleanFilter(
        method="filter_my_invitations",
        widget=BooleanWidget,
    )
    o = django_filters.OrderingFilter(
        fields=(
            "invited_at",
            "expertise_match_score",
            ("open_assignments", "current_assignments"),
            "created",
        )
    )

    def filter_invitation_status(self, queryset, name, value):
        """Match the effective status: a pending invitation past its deadline is expired."""
        if not value:
            return queryset
        return queryset.filter(reviewer_pool_invitation_status_q(value))

    def filter_my_invitations(self, queryset, name, value):
        """Filter to show only invitations for the current user."""
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        return queryset.filter(models.CallReviewerPool.addressed_to(user))

    class Meta:
        model = models.CallReviewerPool
        fields = []


class COIDetectionJobFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    job_type = django_filters.ChoiceFilter(
        choices=models.COIDetectionJob._meta.get_field("job_type").choices
    )
    state = django_filters.MultipleChoiceFilter(
        choices=models.COIDetectionJob._meta.get_field("state").choices
    )
    o = django_filters.OrderingFilter(
        fields=("created", "started_at", "completed_at", "state")
    )

    class Meta:
        model = models.COIDetectionJob
        fields = []


class ReviewerBidFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail", field_name="reviewer__uuid"
    )
    bid = django_filters.MultipleChoiceFilter(
        choices=models.ReviewerBid._meta.get_field("bid").choices
    )
    o = django_filters.OrderingFilter(fields=("submitted_at", "modified_at", "bid"))

    class Meta:
        model = models.ReviewerBid
        fields = []


class ReviewerSuggestionFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail", field_name="reviewer__uuid"
    )
    status = django_filters.MultipleChoiceFilter(
        choices=models.ReviewerSuggestion._meta.get_field("status").choices
    )
    min_affinity_score = django_filters.NumberFilter(
        field_name="affinity_score", lookup_expr="gte"
    )
    o = django_filters.OrderingFilter(
        fields=("affinity_score", "created", "status", "reviewed_at")
    )

    class Meta:
        model = models.ReviewerSuggestion
        fields = []


# =============================================================================
# Assignment Batch Filters
# =============================================================================


class AssignmentBatchFilter(django_filters.FilterSet):
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="call__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail",
        field_name="reviewer_pool_entry__reviewer__uuid",
    )
    reviewer_pool_entry_uuid = core_filters.RelatedUUIDFilter(
        view_name="call-reviewer-pool-detail", field_name="reviewer_pool_entry__uuid"
    )
    status = django_filters.MultipleChoiceFilter(
        choices=models.AssignmentBatch._meta.get_field("status").choices,
        method="filter_status",
    )
    source = django_filters.MultipleChoiceFilter(
        choices=models.AssignmentBatch._meta.get_field("source").choices
    )
    sent_after = django_filters.DateTimeFilter(field_name="sent_at", lookup_expr="gte")
    sent_before = django_filters.DateTimeFilter(field_name="sent_at", lookup_expr="lte")
    o = django_filters.OrderingFilter(
        fields=("created", "sent_at", "expires_at", "status")
    )

    def filter_status(self, queryset, name, value):
        """Match the effective status: a sent batch past its deadline is expired."""
        if not value:
            return queryset
        return queryset.filter(assignment_batch_status_q(value))

    class Meta:
        model = models.AssignmentBatch
        fields = []


class AssignmentItemFilter(django_filters.FilterSet):
    batch_uuid = core_filters.RelatedUUIDFilter(
        view_name="assignment-batch-detail", field_name="batch__uuid"
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="batch__call__uuid"
    )
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="reviewer-profile-detail",
        field_name="batch__reviewer_pool_entry__reviewer__uuid",
    )
    status = django_filters.MultipleChoiceFilter(
        choices=models.AssignmentItem._meta.get_field("status").choices,
        method="filter_status",
    )
    has_coi = django_filters.BooleanFilter()
    min_affinity_score = django_filters.NumberFilter(
        field_name="affinity_score", lookup_expr="gte"
    )
    o = django_filters.OrderingFilter(
        fields=("created", "affinity_score", "status", "responded_at")
    )

    def filter_status(self, queryset, name, value):
        """Match the effective status: a pending item past its deadline is expired."""
        if not value:
            return queryset
        return queryset.filter(assignment_item_status_q(value))

    class Meta:
        model = models.AssignmentItem
        fields = []


class ProposalRankingFilter(django_filters.FilterSet):
    round_uuid = core_filters.RelatedUUIDFilter(
        view_name="call-round-detail", field_name="round__uuid"
    )
    proposal_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="round__call__uuid"
    )
    panel_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-protected-call-panels",
        field_name="proposal__panel__uuid",
    )
    meeting_note_uuid = core_filters.RelatedUUIDFilter(
        view_name="round-meeting-note-detail",
        field_name="meeting_note__uuid",
        help_text=(
            "A round report's ranking; without it the list is the round's "
            "overall ranking."
        ),
    )
    o = django_filters.OrderingFilter(
        fields=(
            "rank",
            "final_score",
            ("proposal__name", "proposal_name"),
            "modified",
        )
    )

    class Meta:
        model = models.ProposalRanking
        fields = []


class RoundMeetingNoteFilter(django_filters.FilterSet):
    round_uuid = core_filters.RelatedUUIDFilter(
        view_name="call-round-detail", field_name="round__uuid"
    )
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="round__call__uuid"
    )
    call_step_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-call-workflow_step-detail",
        field_name="call_step__uuid",
    )
    is_report = django_filters.BooleanFilter(
        field_name="call_step", lookup_expr="isnull", exclude=True
    )

    class Meta:
        model = models.RoundMeetingNote
        fields = []


class CallTemplateFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    is_published = django_filters.BooleanFilter(widget=BooleanWidget)
    o = django_filters.OrderingFilter(fields=("name", "created", "modified"))

    class Meta:
        model = models.CallTemplate
        fields = ["name", "is_published"]
