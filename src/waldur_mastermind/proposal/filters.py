import django_filters
from django.db.models import Q
from django.utils import timezone
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)

from . import models


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
            return queryset.filter(round__cutoff_time__gte=timezone.now()).distinct()
        return queryset

    def filter_offering_uuid(self, queryset, name, value):
        return queryset.filter(offerings__uuid=value).distinct()


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
    name = django_filters.CharFilter(lookup_expr="icontains")
    call_uuid = core_filters.RelatedUUIDFilter(
        view_name="proposal-public-call-detail", field_name="round__call__uuid"
    )
    organization_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="round__call__manager__customer__uuid"
    )
    created_by_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="created_by__uuid"
    )
    my_proposals = django_filters.BooleanFilter(
        method="filter_my_proposals",
        widget=BooleanWidget,
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

    def filter_my_proposals(self, queryset, name, value):
        """Filter to show only proposals created by the current user."""
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        return queryset.filter(created_by=user)

    class Meta:
        model = models.Proposal
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

    class Meta:
        model = models.Review
        fields = []


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
    o = django_filters.OrderingFilter(
        fields=("created", "state", "offering__name", "call__name")
    )
    state = django_filters.MultipleChoiceFilter(choices=RequestedOfferingStates.CHOICES)

    class Meta:
        model = models.RequestedOffering
        fields = []


class RequestedResourceFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
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
    o = django_filters.OrderingFilter(
        fields=(
            "created",
            "offering__name",
            "resource__name",
            "proposal__name",
        )
    )

    class Meta:
        model = models.RequestedResource
        fields = ["created"]


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
        choices=models.CallReviewerPool._meta.get_field("invitation_status").choices
    )
    my_invitations = django_filters.BooleanFilter(
        method="filter_my_invitations",
        widget=BooleanWidget,
    )
    o = django_filters.OrderingFilter(
        fields=(
            "invited_at",
            "expertise_match_score",
            "current_assignments",
            "created",
        )
    )

    def filter_my_invitations(self, queryset, name, value):
        """Filter to show only invitations for the current user."""
        if not value:
            return queryset
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset.none()
        return queryset.filter(
            models.Q(reviewer__user=user) | models.Q(invited_email=user.email)
        )

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
        choices=models.AssignmentBatch._meta.get_field("status").choices
    )
    source = django_filters.MultipleChoiceFilter(
        choices=models.AssignmentBatch._meta.get_field("source").choices
    )
    sent_after = django_filters.DateTimeFilter(field_name="sent_at", lookup_expr="gte")
    sent_before = django_filters.DateTimeFilter(field_name="sent_at", lookup_expr="lte")
    o = django_filters.OrderingFilter(
        fields=("created", "sent_at", "expires_at", "status")
    )

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
        choices=models.AssignmentItem._meta.get_field("status").choices
    )
    has_coi = django_filters.BooleanFilter()
    min_affinity_score = django_filters.NumberFilter(
        field_name="affinity_score", lookup_expr="gte"
    )
    o = django_filters.OrderingFilter(
        fields=("created", "affinity_score", "status", "responded_at")
    )

    class Meta:
        model = models.AssignmentItem
        fields = []
