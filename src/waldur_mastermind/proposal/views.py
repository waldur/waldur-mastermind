import logging
import secrets
from datetime import datetime, timedelta
from typing import cast

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import (
    Avg,
    Count,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    Max,
    OuterRef,
    ProtectedError,
    Q,
    Subquery,
)
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone as timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import decorators, exceptions, mixins, response, status, viewsets
from rest_framework import permissions as rf_permissions

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.mixins import ReviewerChecklistMixin, UserChecklistMixin
from waldur_core.core import validators as core_validators
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.models import User
from waldur_core.core.utils import SubqueryCount
from waldur_core.core.views import (
    ActionMethodMixin,
    ActionsViewSet,
    ReadOnlyActionsViewSet,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
    has_permission,
    permission_factory,
)
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import filters as structure_filters
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers,
)
from waldur_core.structure.models import Customer
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.views import BaseMarketplaceView, PublicViewsetMixin
from waldur_mastermind.proposal import (
    affinity_scoring,
    filters,
    models,
    orcid_service,
    serializers,
    tasks,
    utils,
    workflow_service,
)
from waldur_mastermind.proposal import enums as proposal_enums
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    MANDATORY_STEPS,
    WORKFLOW_STEPS,
    WORKFLOW_STEPS_MAP,
    AffinityMatrixScopes,
    CallStates,
    COIDetectionJobStates,
    COIDetectionJobTypes,
    COIDetectionMethods,
    COISeverityLevels,
    COIStatuses,
    COITypes,
    ProposalStates,
    RequestedOfferingStates,
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
    TransitionModes,
    WorkflowStepInstanceStatuses,
)

from .managers import get_connected_call_organizers, get_connected_calls
from .models import Proposal
from .serializers import ReviewSubmitSerializer

logger = logging.getLogger(__name__)


class CallManagingOrganisationViewSet(
    UserRoleMixin, PublicViewsetMixin, BaseMarketplaceView
):
    lookup_field = "uuid"
    queryset = models.CallManagingOrganisation.objects.all().order_by("customer__name")
    serializer_class = serializers.CallManagingOrganisationSerializer
    filterset_class = filters.CallManagingOrganisationFilter

    def destroy(self, request, *args, **kwargs):
        instance: models.CallManagingOrganisation = self.get_object()
        try:
            self.perform_destroy(instance)
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        except ProtectedError:
            return response.Response(
                {
                    "detail": "Cannot delete this call manager as there are existing connected calls or user permissions."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        description="Return statistics for call managing organisation.",
        request=None,
        responses=serializers.CallManagingOrganisationStatSerializer,
    )
    @decorators.action(detail=True)
    def stats(self, request, uuid=None):
        instance: models.CallManagingOrganisation = self.get_object()
        now = timezone.now()
        one_week_from_now = now + timedelta(weeks=1)

        open_calls = models.Call.objects.filter(
            state=CallStates.ACTIVE, manager=instance
        ).count()
        active_rounds = models.Round.objects.filter(
            cutoff_time__gte=now,
            call__manager=instance,
            call__state=CallStates.ACTIVE,
        ).count()
        accepted_proposals = models.Proposal.objects.filter(
            state=ProposalStates.ACCEPTED,
            round__call__manager=instance,
            round__call__state=CallStates.ACTIVE,
        ).count()
        pending_proposals = models.Proposal.objects.filter(
            state__in=[
                ProposalStates.IN_REVIEW,
                ProposalStates.SUBMITTED,
            ],
            round__call__manager=instance,
            round__call__state=CallStates.ACTIVE,
        ).count()
        pending_review = models.Review.objects.filter(
            state=models.Review.States.SUBMITTED,
            proposal__round__call__manager=instance,
            proposal__round__call__state=CallStates.ACTIVE,
        ).count()

        rounds_closing_in_one_week = models.Round.objects.filter(
            cutoff_time__gte=now,
            cutoff_time__lte=one_week_from_now,
            call__manager=instance,
            call__state=CallStates.ACTIVE,
        ).count()

        calls_closing_in_one_week = models.Call.objects.filter(
            state=CallStates.ACTIVE,
            round__cutoff_time__gte=now,
            round__cutoff_time__lte=one_week_from_now,
            manager=instance,
        ).count()

        offering_requests_pending = models.RequestedOffering.objects.filter(
            state=RequestedOfferingStates.REQUESTED,
            call__manager=instance,
            call__state=CallStates.ACTIVE,
        ).count()

        return response.Response(
            {
                "open_calls": open_calls,
                "active_rounds": active_rounds,
                "accepted_proposals": accepted_proposals,
                "pending_proposals": pending_proposals,
                "pending_review": pending_review,
                "rounds_closing_in_one_week": rounds_closing_in_one_week,
                "calls_closing_in_one_week": calls_closing_in_one_week,
                "offering_requests_pending": offering_requests_pending,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Get call performance statistics across all calls.",
        responses={200: serializers.CallPerformanceStatSerializer(many=True)},
    )
    @decorators.action(detail=False)
    def global_stats_performance(self, request):
        if not request.user.is_staff:
            raise exceptions.PermissionDenied()
        calls = models.Call.objects.all()
        now = timezone.now()
        data = []
        for call in calls:
            proposals = models.Proposal.objects.filter(round__call=call)
            proposals_counts = proposals.aggregate(
                total=Count("id"),
                draft=Count("id", filter=Q(state=ProposalStates.DRAFT)),
                submitted=Count("id", filter=Q(state=ProposalStates.SUBMITTED)),
                in_review=Count("id", filter=Q(state=ProposalStates.IN_REVIEW)),
                accepted=Count("id", filter=Q(state=ProposalStates.ACCEPTED)),
                rejected=Count("id", filter=Q(state=ProposalStates.REJECTED)),
                canceled=Count("id", filter=Q(state=ProposalStates.CANCELED)),
                last_submission=Max(
                    "created",
                    filter=Q(
                        state__in=[
                            ProposalStates.SUBMITTED,
                            ProposalStates.ACCEPTED,
                            ProposalStates.REJECTED,
                        ]
                    ),
                ),
            )

            reviews = models.Review.objects.filter(proposal__round__call=call)
            reviews_stats = reviews.aggregate(
                total=Count("id"),
                completed=Count("id", filter=Q(state=models.Review.States.SUBMITTED)),
                avg_score=Avg(
                    "summary_score", filter=Q(state=models.Review.States.SUBMITTED)
                ),
            )

            active_rounds = call.round_set.filter(
                start_time__lte=now, cutoff_time__gte=now
            ).count()

            accepted = proposals_counts["accepted"]
            rejected = proposals_counts["rejected"]
            total_decided = accepted + rejected
            acceptance_rate = (
                (accepted / total_decided * 100) if total_decided > 0 else 0
            )

            data.append(
                {
                    "call_uuid": call.uuid,
                    "call_name": call.name,
                    "managing_organization_name": call.manager.customer.name,
                    "state": call.state,
                    "total_proposals": proposals_counts["total"],
                    "proposals_draft": proposals_counts["draft"],
                    "proposals_submitted": proposals_counts["submitted"],
                    "proposals_in_review": proposals_counts["in_review"],
                    "proposals_accepted": proposals_counts["accepted"],
                    "proposals_rejected": proposals_counts["rejected"],
                    "proposals_canceled": proposals_counts["canceled"],
                    "acceptance_rate": acceptance_rate,
                    "total_reviews": reviews_stats["total"],
                    "reviews_completed": reviews_stats["completed"],
                    "average_score": reviews_stats["avg_score"],
                    "active_rounds": active_rounds,
                    "last_submission_date": proposals_counts["last_submission"].date()
                    if proposals_counts["last_submission"]
                    else None,
                }
            )

        serializer = serializers.CallPerformanceStatSerializer(data, many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Get review progress statistics across all reviewers.",
        responses={200: serializers.ReviewProgressStatSerializer(many=True)},
    )
    @decorators.action(detail=False)
    def global_stats_review_progress(self, request):
        if not request.user.is_staff:
            raise exceptions.PermissionDenied()
        reviews = models.Review.objects.all()
        reviewers = User.objects.filter(
            id__in=reviews.values_list("reviewer_id", flat=True)
        ).distinct()

        data = []
        for reviewer in reviewers:
            reviewer_reviews = reviews.filter(reviewer=reviewer)
            stats = reviewer_reviews.aggregate(
                total=Count("id"),
                pending=Count("id", filter=Q(state=models.Review.States.IN_REVIEW)),
                completed=Count("id", filter=Q(state=models.Review.States.SUBMITTED)),
                rejected=Count("id", filter=Q(state=models.Review.States.REJECTED)),
                avg_score=Avg(
                    "summary_score", filter=Q(state=models.Review.States.SUBMITTED)
                ),
            )

            # Calculate average review time in days for completed reviews
            completed_reviews = reviewer_reviews.filter(
                state=models.Review.States.SUBMITTED
            )
            durations = completed_reviews.annotate(
                duration=ExpressionWrapper(
                    F("modified") - F("created"), output_field=DurationField()
                )
            ).aggregate(avg_duration=Avg("duration"))

            avg_time = (
                durations["avg_duration"].total_seconds() / 86400
                if durations["avg_duration"]
                else None
            )

            total = stats["total"]
            completion_rate = (stats["completed"] / total * 100) if total > 0 else 0

            data.append(
                {
                    "reviewer_uuid": reviewer.uuid,
                    "reviewer_name": reviewer.full_name,
                    "reviewer_email": reviewer.email,
                    "total_assigned": total,
                    "pending": stats["pending"],
                    "in_progress": stats["pending"],  # For now same as pending
                    "completed": stats["completed"],
                    "declined": stats["rejected"],
                    "average_score": stats["avg_score"],
                    "average_review_time_days": avg_time,
                    "completion_rate": completion_rate,
                }
            )

        serializer = serializers.ReviewProgressStatSerializer(data, many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Get resource demand statistics across all calls and offerings.",
        responses={200: serializers.ResourceDemandStatSerializer(many=True)},
    )
    @decorators.action(detail=False)
    def global_stats_resource_demand(self, request):
        if not request.user.is_staff:
            raise exceptions.PermissionDenied()
        requested_offerings = models.RequestedOffering.objects.all()
        offerings = marketplace_models.Offering.objects.filter(
            id__in=requested_offerings.values_list("offering_id", flat=True)
        ).distinct()

        data = []
        for offering in offerings:
            offer_requests = requested_offerings.filter(offering=offering)
            resources = models.RequestedResource.objects.filter(
                requested_offering__in=offer_requests
            )

            stats = resources.aggregate(
                proposal_count=Count("proposal", distinct=True),
                request_count=Count("id"),
                approved_count=Count(
                    "id", filter=Q(proposal__state=ProposalStates.ACCEPTED)
                ),
                pending_count=Count(
                    "id",
                    filter=Q(
                        proposal__state__in=[
                            ProposalStates.SUBMITTED,
                            ProposalStates.IN_REVIEW,
                        ]
                    ),
                ),
            )

            total_requested_limits = {}
            total_approved_limits = {}

            all_resources = resources.all()
            for res in all_resources:
                limits = res.limits or {}
                for key, val in limits.items():
                    try:
                        fval = float(val)
                        total_requested_limits[key] = (
                            total_requested_limits.get(key, 0) + fval
                        )
                        if res.proposal.state == ProposalStates.ACCEPTED:
                            total_approved_limits[key] = (
                                total_approved_limits.get(key, 0) + fval
                            )
                    except (ValueError, TypeError):
                        continue

            data.append(
                {
                    "offering_uuid": offering.uuid,
                    "offering_name": offering.name,
                    "offering_type": offering.type,
                    "provider_name": offering.customer.name,
                    "proposal_count": stats["proposal_count"],
                    "request_count": stats["request_count"],
                    "approved_count": stats["approved_count"],
                    "pending_count": stats["pending_count"],
                    "total_requested_limits": total_requested_limits,
                    "total_approved_limits": total_approved_limits,
                }
            )

        serializer = serializers.ResourceDemandStatSerializer(data, many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)


class PublicCallViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "uuid"
    queryset = models.Call.objects.filter(
        state__in=[CallStates.ACTIVE, CallStates.ARCHIVED]
    ).order_by("created")
    serializer_class = serializers.PublicCallSerializer
    filterset_class = filters.CallFilter
    permission_classes = (rf_permissions.AllowAny,)

    @extend_schema(
        description="Check if the current user is eligible to submit proposals to this call.",
        request=None,
        responses={200: serializers.EligibilityCheckSerializer},
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        permission_classes=[rf_permissions.IsAuthenticated],
    )
    def check_eligibility(self, request, uuid=None):
        """Check if the current user is eligible to submit proposals to this call."""
        call = self.get_object()
        user = request.user

        try:
            permissions_utils.validate_user_restrictions(call, user)
            data = {"is_eligible": True, "restrictions": []}
        except exceptions.ValidationError as e:
            # Extract restriction messages
            if hasattr(e, "detail"):
                if isinstance(e.detail, list):
                    restrictions = [str(msg) for msg in e.detail]
                elif isinstance(e.detail, dict):
                    restrictions = []
                    for key, value in e.detail.items():
                        if isinstance(value, list):
                            restrictions.extend([str(v) for v in value])
                        else:
                            restrictions.append(str(value))
                else:
                    restrictions = [str(e.detail)]
            else:
                restrictions = [str(e)]
            data = {"is_eligible": False, "restrictions": restrictions}

        serializer = serializers.EligibilityCheckSerializer(data)
        return response.Response(serializer.data)


class ProtectedCallViewSet(UserRoleMixin, ActionsViewSet, ActionMethodMixin):
    lookup_field = "uuid"
    serializer_class = serializers.ProtectedCallSerializer
    filterset_class = filters.CallFilter
    filter_backends = [DjangoFilterBackend]
    destroy_validators = [core_validators.StateValidator(CallStates.DRAFT)]

    queryset = models.Call.objects.all()

    def get_queryset(self):
        return filter_queryset_for_user(
            super().get_queryset(), self.request.user
        ).order_by("created")

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_offerings_list",
        request=None,
        responses=serializers.RequestedOfferingSerializer(many=True),
        description="List offerings for a call.",
        parameters=[
            OpenApiParameter(
                "state", str, OpenApiParameter.QUERY, description="Filter by state"
            ),
        ],
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_offerings_set",
        request=serializers.RequestedOfferingSerializer,
        responses=serializers.RequestedOfferingSerializer,
        description="Create offering for a call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def offerings(self, request, uuid=None):
        if request.method == "GET":
            call = self.get_object()
            queryset = call.requestedoffering_set.all()

            # Apply state filter if provided - only filter with valid states
            state_filter = request.query_params.getlist("state")
            if state_filter:
                # Get valid state values from the enum
                valid_states = {choice[0] for choice in RequestedOfferingStates.CHOICES}
                # Filter to only include valid state values
                valid_state_filter = [s for s in state_filter if s in valid_states]
                if valid_state_filter:
                    queryset = queryset.filter(state__in=valid_state_filter)

            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(
                    page, context=self.get_serializer_context(), many=True
                )
                return self.get_paginated_response(serializer.data)
            serializer = self.get_serializer(
                queryset,
                context=self.get_serializer_context(),
                many=True,
            )
            return response.Response(serializer.data, status=status.HTTP_200_OK)

        return self.action_list_method("requestedoffering_set")(self, request, uuid)

    offerings_serializer_class = serializers.RequestedOfferingSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RequestedOfferingSerializer}
    )
    def offering_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "requestedoffering_set",
            delete_validators=[],
            update_validators=[
                core_validators.StateValidator(
                    models.RequestedOffering.States.REQUESTED
                )
            ],
        )(self, request, uuid, obj_uuid)

    offering_detail_serializer_class = serializers.RequestedOfferingSerializer

    @extend_schema(
        description="Activate a call.",
        request=None,
        responses={200: serializers.MessageResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        call: models.Call = self.get_object()
        if call.round_set.count() == 0:
            raise exceptions.ValidationError(
                _("Call must have a round to be activated.")
            )
        if not call.workflow_steps.filter(is_enabled=True).exists():
            raise exceptions.ValidationError(
                _("Call must have at least one enabled workflow step.")
            )
        enabled_step_ids = set(
            call.workflow_steps.filter(is_enabled=True).values_list("step", flat=True)
        )
        missing = [s for s in MANDATORY_STEPS if s not in enabled_step_ids]
        if missing:
            missing_names = [
                WORKFLOW_STEPS_MAP[s].name if s in WORKFLOW_STEPS_MAP else s
                for s in missing
            ]
            raise exceptions.ValidationError(
                _("Mandatory workflow steps are missing: %s.")
                % ", ".join(missing_names)
            )
        # Require an *accepted* offering: only accepted offerings yield the
        # resource templates applicants can request, and it matches the call
        # serializer's `offerings` field (accepted-only) so the frontend gate
        # agrees exactly with this check.
        if not call.requestedoffering_set.filter(
            state=RequestedOfferingStates.ACCEPTED
        ).exists():
            raise exceptions.ValidationError(
                _("Call must have at least one accepted offering to be activated.")
            )
        call.state = CallStates.ACTIVE
        call.save()
        return response.Response(
            {"message": "Call has been activated."},
            status=status.HTTP_200_OK,
        )

    activate_validators = [
        core_validators.StateValidator(CallStates.DRAFT, CallStates.ARCHIVED)
    ]

    @extend_schema(
        description="Archive a call.",
        request=None,
        responses={200: serializers.MessageResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        call: models.Call = self.get_object()
        call.state = CallStates.ARCHIVED
        call.save()
        return response.Response(
            {"message": "Call has been archived."},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        operation_id="proposal_protected_calls_duplicate",
        description=(
            "Duplicate a call. The new call inherits the source call's "
            "configuration (offerings, rounds, workflow steps, resource "
            "templates, role mappings, documents, and COI/matching/"
            "assignment/applicant-visibility settings) and starts in draft "
            "state. Proposals, reviews, team permissions, and reviewer-pool "
            "memberships are not copied."
        ),
        request=serializers.DuplicateCallRequestSerializer,
        responses={201: serializers.ProtectedCallSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def duplicate(self, request, uuid=None):
        source = self.get_object()
        payload = serializers.DuplicateCallRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        new_name = payload.validated_data.pop("name")
        new_call = utils.duplicate_call(
            source=source,
            new_name=new_name,
            created_by=request.user,
            sections=payload.validated_data,
        )
        return response.Response(
            serializers.ProtectedCallSerializer(
                new_call, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    duplicate_permissions = [
        permission_factory(PermissionEnum.CREATE_CALL, ["manager"])
    ]
    duplicate_serializer_class = serializers.DuplicateCallRequestSerializer

    archive_validators = [
        core_validators.StateValidator(CallStates.DRAFT, CallStates.ACTIVE)
    ]

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_rounds_list",
        request=None,
        responses=serializers.ProtectedRoundSerializer(many=True),
        description="List rounds for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_rounds_set",
        request=serializers.ProtectedRoundSerializer,
        responses=serializers.ProtectedRoundSerializer,
        description="Create a round for a call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def rounds(self, request, uuid=None):
        # TODO: Will be better move this to method of serializer and add tests.
        call: models.Call = self.get_object()
        method = self.request.method

        if method == "POST":
            repeat = request.query_params.get("repeat", "false")
            count = request.query_params.get("count", "1")

            if repeat in ["true", "True"] and int(count) > 1:
                cutoff_time_str = request.data.get("cutoff_time")
                start_time_str = request.data.get("start_time")

                cutoff_time = datetime.strptime(cutoff_time_str, "%Y-%m-%dT%H:%M")
                start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M")

                duration = cutoff_time - start_time
                data = request.data.copy()
                all_created_data = []

                for i in range(int(count)):
                    new_start_time = start_time + i * duration
                    new_cutoff_time = cutoff_time + i * duration

                    data["start_time"] = new_start_time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    data["cutoff_time"] = new_cutoff_time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z"
                    )

                    serializer = self.get_serializer(
                        context=self.get_serializer_context(),
                        data=data,
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save(call=call)
                    all_created_data.append(serializer.data)
                    logger.info(
                        f"Round is created with start_time: {new_start_time}, cutoff_time: {new_cutoff_time}"
                    )
                return response.Response(
                    all_created_data,
                    status=status.HTTP_201_CREATED,
                )
            else:
                serializer = self.get_serializer(
                    context=self.get_serializer_context(),
                    data=self.request.data,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save(call=call)
                return response.Response(
                    serializer.data,
                    status=status.HTTP_201_CREATED,
                )
        queryset = call.round_set.all().order_by("-start_time")
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, context=self.get_serializer_context(), many=True
            )
            return self.get_paginated_response(serializer.data)
        return response.Response(
            self.get_serializer(
                queryset,
                context=self.get_serializer_context(),
                many=True,
            ).data,
            status=status.HTTP_200_OK,
        )

    rounds_serializer_class = serializers.ProtectedRoundSerializer

    @extend_schema(
        operation_id="proposal_protected_calls_rounds_bulk_set",
        description=(
            "Create multiple rounds on a call at a fixed cadence. Spacing is "
            "controlled by ``cadence`` (monthly/quarterly/biannual/yearly/"
            "custom). Each round's ``cutoff_time`` is derived as "
            "``start_time + submission_window_days``. Fixed-date allocation "
            "is not supported in bulk mode."
        ),
        request=serializers.BulkRoundCreateRequestSerializer,
        responses={201: serializers.ProtectedRoundSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["post"], url_path="rounds-bulk-set")
    def rounds_bulk_set(self, request, uuid=None):
        call: models.Call = self.get_object()
        payload = serializers.BulkRoundCreateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        rounds = utils.bulk_create_rounds(call, payload.validated_data)
        return response.Response(
            serializers.ProtectedRoundSerializer(
                rounds, many=True, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    rounds_bulk_set_permissions = [permission_factory(PermissionEnum.UPDATE_CALL)]
    rounds_bulk_set_serializer_class = serializers.BulkRoundCreateRequestSerializer

    @extend_schema(responses={status.HTTP_200_OK: serializers.ProtectedRoundSerializer})
    def round_detail(self, request, uuid=None, obj_uuid=None):
        def validate_call_state(call_round):
            if call_round.call.state == CallStates.ARCHIVED:
                raise IncorrectStateException()

        def validate_existing_of_proposals(call_round):
            if call_round.proposal_set.exclude(
                state__in=[
                    ProposalStates.CANCELED,
                    ProposalStates.REJECTED,
                ]
            ).exists():
                raise IncorrectStateException()

        return self.action_detail_method(
            "round_set",
            delete_validators=[validate_call_state, validate_existing_of_proposals],
            update_validators=[validate_call_state],
        )(self, request, uuid, obj_uuid)

    round_detail_serializer_class = serializers.ProtectedRoundSerializer

    @extend_schema(responses={status.HTTP_200_OK: OpenApiTypes.STR})
    def close_round(self, request, uuid=None, obj_uuid=None):
        call: models.Call = self.get_object()

        try:
            call_round = call.round_set.get(uuid=obj_uuid)
        except models.Round.DoesNotExist:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

        permissions_utils.permission_factory(PermissionEnum.CLOSE_ROUNDS, ["*"])(
            request, self, call
        )

        if call_round.call.state != CallStates.ACTIVE:
            raise exceptions.ValidationError(_("Call is not active."))

        if call_round.start_time > timezone.now():
            call_round.start_time = timezone.now()

        if call_round.cutoff_time < timezone.now():
            call_round.cutoff_time = timezone.now()

        utils.process_closed_round(call_round)

        return response.Response(
            "Round has been closed.",
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.CallAttachDocumentsSerializer,
        responses=None,
        description="Attach documents to call.",
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_documents(self, request, uuid=None):
        instance: models.Call = self.get_object()

        if hasattr(request.data, "getlist"):
            documents = request.data.getlist("documents", [])
        else:
            documents = request.data.get("documents", [])
        description = request.data.get("description", "")

        for file_data in documents:
            obj, created = models.CallDocument.objects.get_or_create(
                call=instance,
                file=file_data,
                description=description,
            )
            if created:
                instance.documents.add(obj)
                event_logger.emit(
                    f"Attachment for call {instance.name} has been added.",
                    event_type=EventType.CALL_DOCUMENT_ADDED,
                    event_context={"call": instance},
                    scopes=[_get_customer(instance)],
                )
                logger.info(f"Attachment for {instance.name} has been added.")

        return response.Response(
            "Documents attached successfully",
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.CallDetachDocumentsSerializer,
        responses=None,
        description="Detach documents from call.",
    )
    @decorators.action(detail=True, methods=["post"])
    def detach_documents(self, request, uuid=None):
        instance: models.Call = self.get_object()
        if hasattr(request.data, "getlist"):
            documents = request.data.getlist("documents", [])
        else:
            documents = request.data.get("documents", [])
        for file_data in documents:
            models.CallDocument.objects.get(
                call=instance,
                uuid=file_data,
            ).delete()
            event_logger.emit(
                f"Attachment for call {instance.name} has been removed.",
                event_type=EventType.CALL_DOCUMENT_REMOVED,
                event_context={"call": instance},
                scopes=[_get_customer(instance)],
            )
            logger.info(f"Attachment for {instance.name} has been removed.")

        return response.Response(
            "Documents removed successfully",
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_resource_templates_list",
        request=None,
        responses=serializers.CallResourceTemplateSerializer(many=True),
        description="List resource templates for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_resource_templates_set",
        request=serializers.CallResourceTemplateSerializer,
        responses=serializers.CallResourceTemplateSerializer,
        description="Create resource template for a call.",
    )
    @extend_schema(responses={status.HTTP_200_OK: dict})
    @decorators.action(detail=True, methods=["get", "post"])
    def resource_templates(self, request, uuid=None):
        return self.action_list_method("resource_templates")(self, request, uuid)

    resource_templates_serializer_class = serializers.CallResourceTemplateSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallResourceTemplateSerializer}
    )
    def resource_template_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "resource_templates", delete_validators=[], update_validators=[]
        )(self, request, uuid, obj_uuid)

    resource_template_detail_serializer_class = (
        serializers.CallResourceTemplateSerializer
    )

    # Workflow Step Configuration Endpoints

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_workflow_steps_list",
        request=None,
        responses=serializers.CallWorkflowStepSerializer(many=True),
        description="List workflow steps for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_workflow_steps_set",
        request=serializers.CallWorkflowStepSerializer,
        responses=serializers.CallWorkflowStepSerializer,
        description="Create or update a workflow step for a call.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def workflow_steps(self, request, uuid=None):
        """GET returns the call's steps sorted by display_order then catalog
        order; POST delegates to action_list_method for the standard
        create/update path."""
        if request.method == "GET":
            call = self.get_object()
            catalog_index = {
                step.id: index
                for index, step in enumerate(proposal_enums.WORKFLOW_STEPS)
            }
            steps = sorted(
                call.workflow_steps.all(),
                key=lambda s: (
                    s.display_order
                    if s.display_order is not None
                    else catalog_index.get(s.step, len(catalog_index)),
                    s.created,
                ),
            )
            serializer = self.get_serializer(
                steps, context=self.get_serializer_context(), many=True
            )
            return response.Response(serializer.data, status=status.HTTP_200_OK)
        return self.action_list_method("workflow_steps")(self, request, uuid)

    workflow_steps_serializer_class = serializers.CallWorkflowStepSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.CallWorkflowStepSerializer}
    )
    def workflow_step_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "workflow_steps", delete_validators=[], update_validators=[]
        )(self, request, uuid, obj_uuid)

    workflow_step_detail_serializer_class = serializers.CallWorkflowStepSerializer

    # Call Manager Compliance Endpoints
    compliance_overview_permissions = [permission_factory(PermissionEnum.UPDATE_CALL)]

    @extend_schema(
        description="Get compliance overview for call manager showing all proposals and their compliance status.",
        responses=serializers.CallComplianceOverviewSerializer,
    )
    @decorators.action(detail=True, methods=["get"])
    def compliance_overview(self, request, uuid=None):
        """Get compliance overview for call manager."""
        call = self.get_object()

        if not call.compliance_checklist:
            return response.Response(
                {
                    "detail": "No compliance checklist configured for this call",
                    "checklist": None,
                    "proposals": [],
                }
            )

        # Serialize the compliance overview
        overview_data = serializers.CallComplianceOverviewSerializer(
            call, context={"request": request}
        ).data

        return response.Response(overview_data)

    review_proposal_compliance_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL)
    ]

    @extend_schema(
        description="Mark proposal compliance as reviewed by call manager.",
        request=serializers.CallComplianceReviewSerializer,
        responses=dict[str, str],
    )
    @decorators.action(detail=True, methods=["post"])
    def review_proposal_compliance(self, request, uuid=None):
        """Mark proposal compliance as reviewed by call manager."""
        call = self.get_object()

        # Validate input
        serializer = serializers.CallComplianceReviewSerializer(
            data=request.data, context={"call": call, "request": request}
        )
        serializer.is_valid(raise_exception=True)

        proposal_uuid = serializer.validated_data["proposal_uuid"]
        review_notes = serializer.validated_data.get("review_notes", "")

        try:
            proposal: Proposal = Proposal.objects.get(
                round__call=call, uuid=proposal_uuid
            )
            completion = proposal.checklist_completion

            completion.reviewed_by = request.user
            completion.reviewed_at = timezone.now()
            completion.review_notes = review_notes
            completion.save()

            return response.Response(
                {
                    "detail": "Compliance review completed successfully",
                    "proposal_uuid": str(proposal_uuid),
                    "reviewed_by": request.user.full_name,
                    "reviewed_at": completion.reviewed_at,
                }
            )

        except models.Proposal.DoesNotExist:
            return response.Response(
                {"detail": "Proposal not found"}, status=status.HTTP_404_NOT_FOUND
            )
        except checklist_models.ChecklistCompletion.DoesNotExist:
            return response.Response(
                {"detail": "Proposal has no compliance checklist"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    proposal_compliance_answers_permissions = [
        permission_factory(PermissionEnum.UPDATE_CALL)
    ]

    @extend_schema(
        description="Get detailed compliance answers for a specific proposal (call managers only).",
        responses=checklist_serializers.AnswerSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="proposal_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the proposal",
            )
        ],
    )
    @decorators.action(
        detail=True,
        methods=["get"],
        url_path="proposals/(?P<proposal_uuid>[^/.]+)/compliance-answers",
    )
    def proposal_compliance_answers(self, request, uuid=None, proposal_uuid=None):
        """Get detailed compliance answers for a specific proposal."""
        call: models.Call = self.get_object()

        try:
            proposal = models.Proposal.objects.get(uuid=proposal_uuid, round__call=call)

            if not hasattr(proposal, "checklist_completion"):
                return response.Response(
                    {"detail": "Proposal has no compliance checklist"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            completion = proposal.checklist_completion
            answers = completion.answers.all().select_related("question", "user")

            answers_data = checklist_serializers.AnswerSerializer(
                answers, many=True, context={"request": request}
            ).data

            return response.Response(
                {
                    "proposal": {
                        "uuid": str(proposal.uuid),
                        "name": proposal.name,
                        "created_by": proposal.created_by.full_name
                        if proposal.created_by
                        else None,
                    },
                    "completion": checklist_serializers.ChecklistCompletionReviewerSerializer(
                        completion, context={"request": request}
                    ).data,
                    "answers": answers_data,
                }
            )

        except models.Proposal.DoesNotExist:
            return response.Response(
                {"detail": "Proposal not found"}, status=status.HTTP_404_NOT_FOUND
            )

    @extend_schema(
        description="Get available compliance checklists for call creation/editing.",
        responses=serializers.AvailableChecklistSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="checklist_type",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by checklist type (default: proposal_compliance)",
                required=False,
            ),
            OpenApiParameter(
                name="customer_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Customer UUID to check permissions for. Required to verify user has CREATE_CALL permission on that customer's call managing organization.",
                required=True,
            ),
        ],
    )
    @decorators.action(detail=False, methods=["get"])
    def available_compliance_checklists(self, request):
        """Get list of available compliance checklists for call managers/organizers."""
        customer_uuid = request.query_params.get("customer_uuid")
        if not customer_uuid:
            raise exceptions.ValidationError(
                {"customer_uuid": "This parameter is required."}
            )

        customer = Customer.objects.filter(uuid=customer_uuid).first()
        if not customer:
            raise exceptions.ValidationError({"customer_uuid": "Customer not found."})

        has_call_managing_org = models.CallManagingOrganisation.objects.filter(
            customer=customer
        ).exists()
        if not has_call_managing_org:
            raise exceptions.ValidationError(
                {
                    "customer_uuid": "Customer does not have a call managing organization."
                }
            )
        checklist_type = request.query_params.get(
            "checklist_type", ChecklistTypes.PROPOSAL_COMPLIANCE
        )

        checklists = (
            checklist_models.Checklist.objects.filter(checklist_type=checklist_type)
            .prefetch_related("questions")
            .order_by("name")
        )

        serializer = serializers.AvailableChecklistSerializer(
            checklists, many=True, context={"request": request}
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @staticmethod
    def _check_available_checklists_permission(request, view, obj=None):
        """Check if user has CREATE_CALL permission on call managing organization."""
        user = request.user
        if user.is_staff:
            return

        customer_uuid = request.query_params.get("customer_uuid")
        if not customer_uuid:
            return

        try:
            customer = Customer.objects.get(uuid=customer_uuid)
            call_managing_org = models.CallManagingOrganisation.objects.get(
                customer=customer
            )
            if not permissions_utils.has_permission(
                user, PermissionEnum.CREATE_CALL, call_managing_org
            ):
                raise exceptions.PermissionDenied(
                    "You do not have permission to create calls for this organization."
                )
        except Customer.DoesNotExist:
            return
        except models.CallManagingOrganisation.DoesNotExist:
            return

    available_compliance_checklists_permissions = [
        _check_available_checklists_permission
    ]

    # =========================================================================
    # Reviewer Pool Management
    # =========================================================================

    @extend_schema(
        methods=["get"],
        operation_id="proposal_protected_calls_reviewer_pool_list",
        responses=serializers.CallReviewerPoolSerializer(many=True),
        description="List reviewer pool members for a call.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_protected_calls_invite_reviewers",
        request=serializers.ReviewerInvitationSerializer,
        responses=serializers.CallReviewerPoolSerializer(many=True),
        description="Invite reviewers to join the call's reviewer pool.",
    )
    @decorators.action(detail=True, methods=["get", "post"], url_path="reviewer-pool")
    def reviewer_pool(self, request, uuid=None):
        """List or invite reviewers to the call's reviewer pool."""
        call = self.get_object()

        if request.method == "GET":
            pool_members = list(
                models.CallReviewerPool.objects.filter(call=call).select_related(
                    "reviewer",
                    "reviewer__user",
                    "invited_by",
                    "invited_user",
                )
            )

            # Build prefetch context to avoid N+1 in serializer
            context = self.get_serializer_context()
            context.update(self._build_pool_serializer_context(pool_members, call))

            serializer = serializers.CallReviewerPoolSerializer(
                pool_members,
                many=True,
                context=context,
            )
            return response.Response(serializer.data)

        # POST - invite reviewers
        serializer = serializers.ReviewerInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reviewer_uuids = serializer.validated_data["reviewer_uuids"]
        max_assignments = serializer.validated_data.get("max_assignments", 5)

        # Bulk fetch all reviewers at once to avoid N+1
        reviewers = models.ReviewerProfile.objects.filter(
            uuid__in=reviewer_uuids
        ).select_related("user")
        reviewers_by_uuid = {str(r.uuid): r for r in reviewers}

        created_memberships = []
        for reviewer_uuid in reviewer_uuids:
            reviewer = reviewers_by_uuid.get(str(reviewer_uuid))
            if not reviewer:
                continue

            membership, created = models.CallReviewerPool.objects.get_or_create(
                call=call,
                reviewer=reviewer,
                defaults={
                    "invited_by": request.user,
                    "max_assignments": max_assignments,
                    "invitation_expires_at": timezone.now() + timedelta(days=14),
                },
            )
            if created:
                created_memberships.append(membership)

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                created_memberships,
                many=True,
                context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def _build_pool_serializer_context(self, pool_members, call):
        """Build prefetched COI and review counts for CallReviewerPoolSerializer."""
        if not pool_members:
            return {}

        reviewer_ids = {pm.reviewer_id for pm in pool_members if pm.reviewer_id}
        user_ids = set()
        for pm in pool_members:
            if pm.reviewer and pm.reviewer.user_id:
                user_ids.add(pm.reviewer.user_id)
            elif pm.invited_user_id:
                user_ids.add(pm.invited_user_id)

        # Prefetch COI counts
        coi_counts = {}
        coi_by_severity = {}
        if reviewer_ids:
            coi_data = (
                models.ConflictOfInterest.objects.filter(
                    reviewer_id__in=reviewer_ids,
                    call=call,
                )
                .values("reviewer_id", "severity")
                .annotate(count=Count("id"))
            )
            for item in coi_data:
                key = (item["reviewer_id"], call.id)
                coi_counts[key] = coi_counts.get(key, 0) + item["count"]
                if key not in coi_by_severity:
                    coi_by_severity[key] = {}
                coi_by_severity[key][item["severity"]] = item["count"]

        # Prefetch review counts
        review_counts = {}
        if user_ids:
            review_data = (
                models.Review.objects.filter(
                    reviewer_id__in=user_ids,
                    proposal__round__call=call,
                )
                .values("reviewer_id", "state")
                .annotate(count=Count("id"))
            )
            for item in review_data:
                key = (item["reviewer_id"], call.id)
                if key not in review_counts:
                    review_counts[key] = {}
                review_counts[key][item["state"]] = item["count"]

        return {
            "coi_counts": coi_counts,
            "coi_by_severity": coi_by_severity,
            "review_counts": review_counts,
        }

    reviewer_pool_serializer_class = serializers.CallReviewerPoolSerializer
    reviewer_pool_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    # =========================================================================
    # Reviewer Discovery & Email Invitations
    # =========================================================================

    @extend_schema(
        description="Invite a reviewer by email address. Creates an invitation that requires the reviewer to create and publish a profile before accepting.",
        request=serializers.EmailInvitationSerializer,
        responses=serializers.CallReviewerPoolSerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="invite-by-email")
    def invite_by_email(self, request, uuid=None):
        """Invite a reviewer by email address."""
        call = self.get_object()

        serializer = serializers.EmailInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        User = get_user_model()

        # Check if invitation already exists for this email
        existing = models.CallReviewerPool.objects.filter(
            call=call, invited_email__iexact=email
        ).first()
        if existing:
            return response.Response(
                {"detail": _("An invitation for this email already exists.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if user with this email exists
        user = User.objects.filter(email__iexact=email).first()
        reviewer = None
        if user:
            reviewer = models.ReviewerProfile.objects.filter(user=user).first()
            # Check if already in pool via reviewer FK
            if (
                reviewer
                and models.CallReviewerPool.objects.filter(
                    call=call, reviewer=reviewer
                ).exists()
            ):
                return response.Response(
                    {"detail": _("This reviewer is already in the pool.")},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Create invitation
        pool_member = models.CallReviewerPool.objects.create(
            call=call,
            reviewer=reviewer,  # May be None
            invited_email=email,
            invited_user=user,  # May be None
            invited_by=request.user,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        tasks.send_reviewer_invitation_email.delay(pool_member.uuid)

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                pool_member, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    invite_by_email_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Generate reviewer suggestions with configurable matching source.",
        request=serializers.GenerateSuggestionsRequestSerializer,
        responses={200: serializers.GenerateSuggestionsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="generate-suggestions")
    def generate_suggestions(self, request, uuid=None):
        """
        Run affinity algorithm on published profiles to generate suggestions.

        Request body options:
        - source: One of 'call_description', 'all_proposals', 'selected_proposals', 'custom_keywords'
        - proposal_uuids: List of proposal UUIDs (for 'selected_proposals' source)
        - keywords: List of keyword strings (for 'custom_keywords' source)
        - keyword_search_mode: 'expertise_only' or 'full_text' (for 'custom_keywords' source)
        - min_affinity_threshold: Override minimum threshold (0-1)
        """
        call = self.get_object()

        # Parse request body if provided
        if request.data:
            req_serializer = serializers.GenerateSuggestionsRequestSerializer(
                data=request.data
            )
            req_serializer.is_valid(raise_exception=True)
            data = req_serializer.validated_data

            result = affinity_scoring.compute_suggestions_for_call_configurable(
                call=call,
                source=data.get("source", "all_proposals"),
                proposal_uuids=data.get("proposal_uuids"),
                keywords=data.get("keywords"),
                keyword_search_mode=data.get("keyword_search_mode", "expertise_only"),
                min_threshold=data.get("min_affinity_threshold"),
            )
        else:
            # Backward compatibility: no body means use all_proposals
            result = affinity_scoring.compute_suggestions_for_call_configurable(
                call=call,
                source="all_proposals",
            )

        return response.Response(result, status=status.HTTP_200_OK)

    generate_suggestions_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="List all reviewer suggestions for this call with affinity scores.",
        responses=serializers.ReviewerSuggestionSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"])
    def suggestions(self, request, uuid=None):
        """List reviewer suggestions for this call."""
        call = self.get_object()
        suggestions = models.ReviewerSuggestion.objects.filter(call=call)

        # Apply status filter if provided
        status_filter = request.query_params.getlist("status")
        if status_filter:
            suggestions = suggestions.filter(status__in=status_filter)

        serializer = serializers.ReviewerSuggestionSerializer(
            suggestions,
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data)

    suggestions_serializer_class = serializers.ReviewerSuggestionSerializer
    suggestions_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Send invitations to all confirmed suggestions.",
        request=None,
        responses={200: serializers.SendInvitationsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="send-invitations")
    def send_invitations(self, request, uuid=None):
        """Send invitations to all confirmed suggestions."""
        call = self.get_object()

        confirmed_suggestions = list(
            models.ReviewerSuggestion.objects.filter(
                call=call,
                status=ReviewerSuggestionStatuses.CONFIRMED,
            ).select_related("reviewer")
        )

        if not confirmed_suggestions:
            return response.Response(
                {"invitations_sent": 0},
                status=status.HTTP_200_OK,
            )

        # Prefetch existing pool members to avoid N+1 existence checks
        existing_pool_reviewer_ids = set(
            models.CallReviewerPool.objects.filter(
                call=call,
                reviewer_id__in=[s.reviewer_id for s in confirmed_suggestions],
            ).values_list("reviewer_id", flat=True)
        )

        # Prepare bulk operations
        invitations_to_create = []
        suggestions_to_update = []

        for suggestion in confirmed_suggestions:
            # Skip if already in pool
            if suggestion.reviewer_id in existing_pool_reviewer_ids:
                continue

            # Prepare invitation for bulk create
            invitations_to_create.append(
                models.CallReviewerPool(
                    call=call,
                    reviewer=suggestion.reviewer,
                    invited_by=request.user,
                    invitation_status=ReviewerPoolInvitationStatuses.PENDING,
                    expertise_match_score=suggestion.affinity_score,
                )
            )

            # Mark suggestion for update
            suggestion.status = ReviewerSuggestionStatuses.INVITED
            suggestions_to_update.append(suggestion)

        # Bulk create invitations
        if invitations_to_create:
            models.CallReviewerPool.objects.bulk_create(invitations_to_create)

        # Bulk update suggestion statuses
        if suggestions_to_update:
            models.ReviewerSuggestion.objects.bulk_update(
                suggestions_to_update, fields=["status"]
            )

        return response.Response(
            {"invitations_sent": len(invitations_to_create)},
            status=status.HTTP_200_OK,
        )

    send_invitations_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    # =========================================================================
    # COI Management
    # =========================================================================

    @extend_schema(
        description="Get COI configuration for this call.",
        responses=serializers.CallCOIConfigurationSerializer,
    )
    @decorators.action(
        detail=True, methods=["get", "patch"], url_path="coi-configuration"
    )
    def coi_configuration(self, request, uuid=None):
        """Get or update COI configuration for a call."""
        call = self.get_object()

        config, created = models.CallCOIConfiguration.objects.get_or_create(call=call)

        if request.method == "GET":
            serializer = serializers.CallCOIConfigurationSerializer(
                config, context=self.get_serializer_context()
            )
            return response.Response(serializer.data)

        # PATCH - update configuration
        serializer = serializers.CallCOIConfigurationSerializer(
            config,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data)

    coi_configuration_serializer_class = serializers.CallCOIConfigurationSerializer
    coi_configuration_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="List all conflicts of interest detected for this call.",
        responses=serializers.ConflictOfInterestSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"])
    def conflicts(self, request, uuid=None):
        """List all COIs for a call."""
        call = self.get_object()
        conflicts = models.ConflictOfInterest.objects.filter(call=call).order_by(
            "-detected_at"
        )

        # Apply filters from query params
        status_filter = request.query_params.getlist("status")
        if status_filter:
            conflicts = conflicts.filter(status__in=status_filter)

        severity_filter = request.query_params.get("severity")
        if severity_filter:
            conflicts = conflicts.filter(severity=severity_filter)

        serializer = serializers.ConflictOfInterestSerializer(
            conflicts,
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data)

    conflicts_serializer_class = serializers.ConflictOfInterestSerializer
    conflicts_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Get summary statistics of conflicts for this call.",
        responses={200: serializers.ConflictSummaryResponseSerializer},
    )
    @decorators.action(detail=True, methods=["get"], url_path="conflict-summary")
    def conflict_summary(self, request, uuid=None):
        """Get summary statistics of COIs for a call."""
        call = self.get_object()

        # Use aggregation queries instead of iterating queryset multiple times
        # Count by status
        status_counts = (
            models.ConflictOfInterest.objects.filter(call=call)
            .values("status")
            .annotate(count=Count("id"))
        )
        by_status = {item["status"]: item["count"] for item in status_counts}

        # Count by severity
        severity_counts = (
            models.ConflictOfInterest.objects.filter(call=call)
            .values("severity")
            .annotate(count=Count("id"))
        )
        by_severity = {item["severity"]: item["count"] for item in severity_counts}

        # Count by type
        type_counts = (
            models.ConflictOfInterest.objects.filter(call=call)
            .values("coi_type")
            .annotate(count=Count("id"))
        )
        by_type = {item["coi_type"]: item["count"] for item in type_counts}

        # Calculate total
        total = sum(by_status.values())

        return response.Response(
            {
                "total": total,
                "by_status": by_status,
                "by_severity": by_severity,
                "by_type": by_type,
            }
        )

    conflict_summary_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Trigger automated COI detection for all reviewer-proposal pairs.",
        request=serializers.TriggerCOIDetectionSerializer,
        responses=serializers.COIDetectionJobSerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="detect-conflicts")
    def detect_conflicts(self, request, uuid=None):
        """Trigger COI detection job for this call."""
        call = self.get_object()

        serializer = serializers.TriggerCOIDetectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        job_type = serializer.validated_data.get(
            "job_type", COIDetectionJobTypes.FULL_CALL
        )

        # Create detection job
        job = models.COIDetectionJob.objects.create(
            call=call,
            job_type=job_type,
            state=COIDetectionJobStates.PENDING,
        )

        # Run detection as background Celery task
        tasks.run_coi_detection.delay(str(job.uuid))

        return response.Response(
            serializers.COIDetectionJobSerializer(
                job, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )

    detect_conflicts_serializer_class = serializers.TriggerCOIDetectionSerializer
    detect_conflicts_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    # =========================================================================
    # Matching
    # =========================================================================

    @extend_schema(
        description="Get or update matching configuration for this call.",
        responses=serializers.MatchingConfigurationSerializer,
    )
    @decorators.action(
        detail=True, methods=["get", "patch"], url_path="matching-configuration"
    )
    def matching_configuration(self, request, uuid=None):
        """Get or update matching configuration for a call."""
        call = self.get_object()

        config, created = models.MatchingConfiguration.objects.get_or_create(call=call)

        if request.method == "GET":
            serializer = serializers.MatchingConfigurationSerializer(
                config, context=self.get_serializer_context()
            )
            return response.Response(serializer.data)

        # PATCH - update configuration
        serializer = serializers.MatchingConfigurationSerializer(
            config,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(serializer.data)

    matching_configuration_serializer_class = (
        serializers.MatchingConfigurationSerializer
    )
    matching_configuration_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        request=None,
        description="Compute affinity scores for all reviewer-proposal pairs.",
        responses={200: serializers.ComputeAffinitiesResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="compute-affinities")
    def compute_affinities(self, request, uuid=None):
        """Compute affinity scores for reviewer-proposal matching."""
        call = self.get_object()

        affinities = affinity_scoring.compute_affinities_for_call(call)

        return response.Response(
            {
                "computed_count": len(affinities),
                "message": f"Computed {len(affinities)} affinity scores.",
            }
        )

    compute_affinities_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Get affinity matrix for reviewer-proposal matching.",
        responses={200: serializers.AffinityMatrixResponseSerializer},
        parameters=[
            OpenApiParameter(
                "scope",
                str,
                OpenApiParameter.QUERY,
                description="Filter by reviewer source: 'pool' (accepted reviewers), 'suggestions' (suggested reviewers), or 'all' (both). Default: 'pool'",
                enum=AffinityMatrixScopes.VALUES,
            ),
        ],
    )
    @decorators.action(detail=True, methods=["get"], url_path="affinity-matrix")
    def affinity_matrix(self, request, uuid=None):
        """Get affinity matrix for this call."""
        call = self.get_object()

        scope = request.query_params.get("scope", AffinityMatrixScopes.POOL)
        if scope not in AffinityMatrixScopes.VALUES:
            scope = AffinityMatrixScopes.POOL

        matrix = affinity_scoring.get_affinity_matrix(call, scope=scope)
        return response.Response(matrix)

    affinity_matrix_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Get proposed reviewer-proposal assignments.",
        responses=serializers.ProposedAssignmentSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"], url_path="proposed-assignments")
    def proposed_assignments(self, request, uuid=None):
        """Get proposed assignments for this call."""
        call = self.get_object()

        assignments = models.ProposedAssignment.objects.filter(call=call).order_by(
            "proposal", "-affinity_score"
        )

        # Filter by deployment status
        is_deployed = request.query_params.get("is_deployed")
        if is_deployed is not None:
            assignments = assignments.filter(is_deployed=is_deployed.lower() == "true")

        serializer = serializers.ProposedAssignmentSerializer(
            assignments,
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data)

    proposed_assignments_serializer_class = serializers.ProposedAssignmentSerializer
    proposed_assignments_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Generate assignment batches for reviewers. "
        "Uses the affinity matrix and COI records to assign reviewers to proposals.",
        request=serializers.GenerateAssignmentsSerializer,
        responses={200: serializers.GenerateAssignmentsResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="generate-assignments")
    def generate_assignments(self, request, uuid=None):
        """
        Generate assignment batches for reviewers.

        This creates draft AssignmentBatch records with AssignmentItems for each
        reviewer-proposal pair. The call manager can then review and send them.
        """
        call = self.get_object()

        serializer = serializers.GenerateAssignmentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        proposal_uuids = serializer.validated_data.get("proposal_uuids", [])
        reviewers_per_proposal = serializer.validated_data.get("reviewers_per_proposal")

        # Use call's matching configuration if not specified
        if not reviewers_per_proposal:
            matching_config = getattr(call, "matching_configuration", None)
            if matching_config:
                reviewers_per_proposal = matching_config.min_reviewers_per_proposal
            else:
                reviewers_per_proposal = 2

        # Get proposals needing assignments
        if proposal_uuids:
            proposals = models.Proposal.objects.filter(
                uuid__in=proposal_uuids,
                round__call=call,
                state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW],
            )
        else:
            proposals = models.Proposal.objects.filter(
                round__call=call,
                state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW],
            )

        # Get accepted pool members
        pool_entries = models.CallReviewerPool.objects.filter(
            call=call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            reviewer__isnull=False,  # Must have a profile
        )

        if not pool_entries.exists():
            return response.Response(
                {
                    "batches_created": 0,
                    "items_created": 0,
                    "proposals_processed": 0,
                    "skipped_proposals": [
                        {
                            "reason": _("No accepted reviewers in pool"),
                        }
                    ],
                },
                status=status.HTTP_200_OK,
            )

        batches_created = 0
        items_created = 0
        proposals_processed = 0
        skipped_proposals = []

        # Convert to list for multiple iterations
        proposals_list = list(proposals)
        proposal_ids = [p.id for p in proposals_list]

        # Prefetch all blocking COIs for all proposals at once
        # Build lookup: proposal_id -> set of reviewer_ids with blocking COI
        blocking_cois = models.ConflictOfInterest.objects.filter(
            proposal_id__in=proposal_ids,
            status__in=["pending", "recused"],
        ).values_list("proposal_id", "reviewer_id")
        blocking_coi_by_proposal = {}
        for proposal_id, reviewer_id in blocking_cois:
            blocking_coi_by_proposal.setdefault(proposal_id, set()).add(reviewer_id)

        # Prefetch all existing assignments for all proposals at once
        # Build lookup: proposal_id -> set of pool_entry_ids already assigned
        existing_items = models.AssignmentItem.objects.filter(
            proposal_id__in=proposal_ids,
        ).values_list("proposal_id", "batch__reviewer_pool_entry_id")
        existing_by_proposal = {}
        for proposal_id, pool_entry_id in existing_items:
            existing_by_proposal.setdefault(proposal_id, set()).add(pool_entry_id)

        # Prefetch all affinities for all proposals at once
        # Build lookup: proposal_id -> list of (reviewer_id, affinity_score)
        affinities_qs = (
            models.ReviewerProposalAffinity.objects.filter(
                proposal_id__in=proposal_ids,
            )
            .order_by("-affinity_score")
            .values_list("proposal_id", "reviewer_id", "affinity_score")
        )
        affinities_by_proposal = {}
        for proposal_id, reviewer_id, score in affinities_qs:
            affinities_by_proposal.setdefault(proposal_id, []).append(
                (reviewer_id, score)
            )

        # Build pool entries lookup by reviewer_id for O(1) access
        pool_entries_list = list(pool_entries.select_related("reviewer"))
        pool_entries_by_reviewer = {
            entry.reviewer_id: entry for entry in pool_entries_list
        }

        # Prefetch all COI records for setting on items later
        # Build lookup: (reviewer_id, proposal_id) -> list of COI objects
        all_coi_records = models.ConflictOfInterest.objects.filter(
            proposal_id__in=proposal_ids,
            status__in=["pending", "recused"],
        )
        coi_by_reviewer_proposal = {}
        for coi in all_coi_records:
            key = (coi.reviewer_id, coi.proposal_id)
            coi_by_reviewer_proposal.setdefault(key, []).append(coi)

        # Track batches and items created during this run for deduplication
        created_batches = {}  # (pool_entry_id,) -> batch
        created_items = set()  # (batch_id, proposal_id)

        # For each proposal, select reviewers
        for proposal in proposals_list:
            proposal_id = proposal.id

            # Get blocking COI reviewer IDs from prefetched data
            blocking_reviewer_ids = blocking_coi_by_proposal.get(proposal_id, set())

            # Get existing assignment pool entry IDs from prefetched data
            existing_pool_entry_ids = existing_by_proposal.get(proposal_id, set())

            # Filter eligible pool entries using prefetched data
            eligible_entries = [
                entry
                for entry in pool_entries_list
                if entry.reviewer_id not in blocking_reviewer_ids
                and entry.id not in existing_pool_entry_ids
            ]

            if not eligible_entries:
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": _("No eligible reviewers available"),
                    }
                )
                continue

            eligible_reviewer_ids = {e.reviewer_id for e in eligible_entries}

            # Get affinity scores from prefetched data, filtered to eligible reviewers
            proposal_affinities = [
                (reviewer_id, score)
                for reviewer_id, score in affinities_by_proposal.get(proposal_id, [])
                if reviewer_id in eligible_reviewer_ids
            ]

            # Select top reviewers by affinity
            selected_entries = []
            selected_entry_ids = set()
            for reviewer_id, affinity_score in proposal_affinities[
                :reviewers_per_proposal
            ]:
                entry = pool_entries_by_reviewer.get(reviewer_id)
                if (
                    entry
                    and entry.id not in selected_entry_ids
                    and entry.current_assignments < (entry.max_assignments or 999)
                ):
                    selected_entries.append((entry, affinity_score))
                    selected_entry_ids.add(entry.id)

            # If we don't have enough from affinity, add more from eligible pool
            if len(selected_entries) < reviewers_per_proposal:
                for entry in eligible_entries:
                    if entry.id in selected_entry_ids:
                        continue
                    if entry.max_assignments is None or (
                        entry.current_assignments < entry.max_assignments
                    ):
                        selected_entries.append((entry, None))
                        selected_entry_ids.add(entry.id)
                        if len(selected_entries) >= reviewers_per_proposal:
                            break

            if not selected_entries:
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": _("Could not find enough eligible reviewers"),
                    }
                )
                continue

            # Create assignment items for each selected reviewer
            for entry, affinity_score in selected_entries:
                # Find or create draft batch for this reviewer
                batch_key = entry.id
                if batch_key in created_batches:
                    batch = created_batches[batch_key]
                    created = False
                else:
                    batch, created = models.AssignmentBatch.objects.get_or_create(
                        call=call,
                        reviewer_pool_entry=entry,
                        status=models.AssignmentBatchStatuses.DRAFT,
                        defaults={
                            "source": models.AssignmentSources.ALGORITHM,
                            "created_by": request.user,
                        },
                    )
                    created_batches[batch_key] = batch
                if created:
                    batches_created += 1

                # Check for existing item using in-memory tracking
                item_key = (batch.id, proposal.id)
                if item_key not in created_items:
                    # Get COI records from prefetched data
                    coi_key = (entry.reviewer_id, proposal.id)
                    coi_list = coi_by_reviewer_proposal.get(coi_key, [])
                    has_coi = len(coi_list) > 0

                    item = models.AssignmentItem.objects.create(
                        batch=batch,
                        proposal=proposal,
                        affinity_score=affinity_score,
                        has_coi=has_coi,
                        status=models.AssignmentItemStatuses.COI_BLOCKED
                        if has_coi
                        else models.AssignmentItemStatuses.PENDING,
                    )
                    if has_coi:
                        item.coi_records.set(coi_list)

                    created_items.add(item_key)
                    items_created += 1

            proposals_processed += 1

        return response.Response(
            {
                "batches_created": batches_created,
                "items_created": items_created,
                "proposals_processed": proposals_processed,
                "skipped_proposals": skipped_proposals,
            },
            status=status.HTTP_200_OK,
        )

    generate_assignments_serializer_class = serializers.GenerateAssignmentsSerializer
    generate_assignments_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Send all draft assignment batches for this call.",
        request=serializers.SendAllAssignmentBatchesSerializer,
        responses={200: serializers.SendAllAssignmentBatchesResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="send-all-assignments")
    def send_all_assignments(self, request, uuid=None):
        """Send all draft assignment batches for this call."""
        call = self.get_object()

        serializer = serializers.SendAllAssignmentBatchesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        batch_uuids = serializer.validated_data.get("batch_uuids", [])

        if batch_uuids:
            batches = models.AssignmentBatch.objects.filter(
                uuid__in=batch_uuids,
                call=call,
                status=models.AssignmentBatchStatuses.DRAFT,
            )
        else:
            batches = models.AssignmentBatch.objects.filter(
                call=call,
                status=models.AssignmentBatchStatuses.DRAFT,
            )

        sent_count = 0
        skipped_count = 0

        for batch in batches:
            try:
                batch.send_invitation(user=request.user)
                sent_count += 1
            except Exception:
                skipped_count += 1

        return response.Response(
            {
                "batches_sent": sent_count,
                "skipped": skipped_count,
            },
            status=status.HTTP_200_OK,
        )

    send_all_assignments_serializer_class = (
        serializers.SendAllAssignmentBatchesSerializer
    )
    send_all_assignments_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]

    @extend_schema(
        description="Create a manual assignment batch for a specific reviewer. "
        "This allows call managers to manually assign proposals to reviewers.",
        request=serializers.CreateManualAssignmentSerializer,
        responses={200: serializers.CreateManualAssignmentResponseSerializer},
    )
    @decorators.action(
        detail=True, methods=["post"], url_path="create-manual-assignment"
    )
    def create_manual_assignment(self, request, uuid=None):
        """
        Create a manual assignment batch for a specific reviewer.
        Creates a draft batch that can be reviewed before sending.
        """
        call = self.get_object()

        serializer = serializers.CreateManualAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pool_entry_uuid = serializer.validated_data["reviewer_pool_entry_uuid"]
        proposal_uuids = serializer.validated_data["proposal_uuids"]
        manager_notes = serializer.validated_data.get("manager_notes", "")

        # Get the reviewer pool entry
        try:
            pool_entry = models.CallReviewerPool.objects.get(
                uuid=pool_entry_uuid,
                call=call,
                invitation_status=models.ReviewerPoolInvitationStatuses.ACCEPTED,
            )
        except models.CallReviewerPool.DoesNotExist:
            raise exceptions.ValidationError(
                {
                    "reviewer_pool_entry_uuid": _(
                        "Reviewer not found in pool or not accepted."
                    )
                }
            )

        # Get proposals
        proposals = models.Proposal.objects.filter(
            uuid__in=proposal_uuids,
            round__call=call,
        )

        if not proposals.exists():
            raise exceptions.ValidationError(
                {"proposal_uuids": _("No valid proposals found.")}
            )

        # Create or get existing draft batch for this reviewer
        batch, batch_created = models.AssignmentBatch.objects.get_or_create(
            call=call,
            reviewer_pool_entry=pool_entry,
            status=models.AssignmentBatchStatuses.DRAFT,
            defaults={
                "source": models.AssignmentSources.MANUAL,
                "manager_notes": manager_notes,
                "created_by": request.user,
            },
        )

        # If batch already existed, update notes if provided
        if not batch_created and manager_notes:
            batch.manager_notes = manager_notes
            batch.save(update_fields=["manager_notes"])

        items_created = 0
        skipped_proposals = []

        for proposal in proposals:
            # Check if assignment already exists
            if models.AssignmentItem.objects.filter(
                batch__reviewer_pool_entry=pool_entry,
                proposal=proposal,
                status__in=[
                    models.AssignmentItemStatuses.PENDING,
                    models.AssignmentItemStatuses.ACCEPTED,
                ],
            ).exists():
                skipped_proposals.append(
                    {
                        "proposal_uuid": str(proposal.uuid),
                        "proposal_name": proposal.name,
                        "reason": _("Assignment already exists for this reviewer"),
                    }
                )
                continue

            # Check for blocking COI
            coi_records = models.ConflictOfInterest.objects.filter(
                reviewer=pool_entry.reviewer,
                proposal=proposal,
                status__in=["pending", "recused"],
            )

            # Create assignment item
            item = models.AssignmentItem.objects.create(
                batch=batch,
                proposal=proposal,
                affinity_score=None,  # Manual assignment - no affinity
                has_coi=coi_records.exists(),
                status=models.AssignmentItemStatuses.COI_BLOCKED
                if coi_records.exists()
                else models.AssignmentItemStatuses.PENDING,
            )
            if coi_records.exists():
                item.coi_records.set(coi_records)

            items_created += 1

        return response.Response(
            {
                "batch_uuid": str(batch.uuid),
                "items_created": items_created,
                "skipped_proposals": skipped_proposals,
            },
            status=status.HTTP_200_OK,
        )

    create_manual_assignment_serializer_class = (
        serializers.CreateManualAssignmentSerializer
    )
    create_manual_assignment_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["*", "manager"],
        )
    ]


def _terminal_workflow_detail(proposal_state):
    """Human-readable detail for a workflow that reached its terminal step."""
    return {
        ProposalStates.ACCEPTED: "Workflow completed. Proposal accepted.",
        ProposalStates.REJECTED: "Workflow completed. Proposal rejected.",
        ProposalStates.CANCELED: "Workflow completed. Proposal canceled.",
    }.get(proposal_state, "Workflow completed.")


class ProposalViewSet(
    UserChecklistMixin,
    ReviewerChecklistMixin,
    UserRoleMixin,
    ActionsViewSet,
    ActionMethodMixin,
):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalSerializer
    filterset_class = filters.ProposalFilter
    disabled_actions = ["update", "partial_update"]
    model = models.Proposal

    def get_queryset(self):
        # Annotate the inputs to workflow_service.is_awaiting_manual_advance so
        # ProposalSerializer.awaiting_manual_advance is computable without a
        # per-row query (N+1 on list). Keep these expressions in sync with
        # workflow_service.is_awaiting_manual_advance.
        latest_step_status = (
            models.ProposalWorkflowStepInstance.objects.filter(
                proposal=OuterRef("pk"), step=OuterRef("workflow_step")
            )
            .order_by("-created")
            .values("status")[:1]
        )
        return (
            filter_queryset_for_user(models.Proposal.objects.all(), self.request.user)
            .annotate(
                _awaiting_manual_step=Exists(
                    models.CallWorkflowStep.objects.filter(
                        call=OuterRef("round__call"),
                        step=OuterRef("workflow_step"),
                        transition_mode=TransitionModes.MANUAL,
                    )
                ),
                _latest_step_status=Subquery(latest_step_status),
            )
            .order_by("created")
        )

    def can_view_scope_team(self, user, proposal):
        # Core walks the proposal's customer/project tree, which misses users
        # whose only role is directly on the call. Call managers — and the
        # reviewers/panel members assigned to the call — may view the proposal
        # team read-only, so the review interface renders (rather than crashing
        # its team section on a 403) and evaluators can comment on it.
        if super().can_view_scope_team(user, proposal):
            return True
        call_id = proposal.round.call_id
        return any(
            call_id in get_connected_calls(user, role)
            for role in (CallRole.MANAGER, CallRole.REVIEWER, CallRole.PANEL_MEMBER)
        )

    # Both mixins use the default implementation (obj.checklist_completion)
    # UserChecklistMixin permissions - for proposal managers only
    # Checklist viewing: same permission as viewing proposal
    def _checklist_view_permission(request, view, obj=None):
        if not obj:
            return

        user = request.user
        if user.is_staff:
            return

        if permissions_utils.has_permission(
            request, PermissionEnum.MANAGE_PROPOSAL, obj
        ):
            return

        if permissions_utils.has_permission(
            request, PermissionEnum.LIST_PROPOSALS, obj.round.call
        ):
            return

        if permissions_utils.has_permission(
            request, PermissionEnum.LIST_PROPOSALS, obj.round.call.manager
        ):
            return

        # Check if user is a call manager
        if obj.round.call_id in get_connected_calls(user, CallRole.MANAGER):
            return

        raise exceptions.PermissionDenied(
            "You do not have permission to view proposal checklist."
        )

    checklist_permissions = [_checklist_view_permission]
    completion_status_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL)]
    # Only proposal managers can submit answers
    submit_answers_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL)]

    # ReviewerChecklistMixin permissions - for proposal reviewers
    # Custom permission for compliance checklists (call managers only) or regular proposal review permissions
    def _compliance_checklist_permission(self, request, view, obj=None):
        """Custom permission that restricts compliance checklist access to call managers only."""
        if not obj:
            return False

        completion = self.get_checklist_completion(obj)
        if completion and completion.checklist:
            if (
                completion.checklist.checklist_type
                == ChecklistTypes.PROPOSAL_COMPLIANCE
            ):
                # For compliance checklists, only call managers can access
                return UserRole.objects.filter(
                    user=request.user,
                    role=CallRole.MANAGER,
                    scope=obj.round.call,
                    is_active=True,
                ).exists()

        # For non-compliance checklists, use regular proposal review permissions
        return permissions_utils.has_permission(
            request, PermissionEnum.MANAGE_PROPOSAL_REVIEW, obj.round.call
        )

    checklist_review_permissions = [_compliance_checklist_permission]
    completion_review_status_permissions = [_compliance_checklist_permission]

    def is_creator(request, view, obj=None):
        if not obj:
            return
        user = request.user
        if obj.created_by == user or user.is_staff:
            return
        raise exceptions.PermissionDenied()

    destroy_permissions = update_project_details_permissions = [is_creator]

    destroy_validators = update_project_details_validators = [
        core_validators.StateValidator(ProposalStates.DRAFT)
    ]

    update_project_details_serializer_class = (
        serializers.ProposalUpdateProjectDetailsSerializer
    )

    @extend_schema(
        description="Update project details of a proposal.",
        request=serializers.ProposalUpdateProjectDetailsSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_project_details(self, request, uuid=None):
        proposal = self.get_object()
        serializer = self.get_serializer(data=request.data, instance=proposal)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return response.Response(status=status.HTTP_200_OK)

    @staticmethod
    def validate_resource_requests_existing(proposal):
        if not models.RequestedResource.objects.filter(proposal=proposal).exists():
            raise exceptions.ValidationError(
                _(
                    "There must be at least some resource requests existing before moving to team validation."
                )
            )

    @extend_schema(
        description="Submit a proposal.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        proposal = self.get_object()

        # The whole block (step instances + proposal state) must be atomic —
        # a partial failure would leave orphan instances and wedge the
        # (proposal, step) unique constraint on retry. The row lock + state
        # re-check serialises concurrent submits that both passed the outer
        # StateValidator before either had committed.
        with transaction.atomic():
            locked = models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            if locked.state != ProposalStates.DRAFT:
                return response.Response(
                    {"detail": "Proposal has already been submitted."},
                    status=status.HTTP_409_CONFLICT,
                )
            proposal = locked
            previous_state = proposal.state

            call = proposal.round.call
            enabled_steps = list(
                models.CallWorkflowStep.objects.filter(call=call, is_enabled=True)
            )
            enabled_step_ids = {s.step for s in enabled_steps}
            first_step_id = next(
                (s.id for s in WORKFLOW_STEPS if s.id in enabled_step_ids), None
            )

            instances_to_create = [
                models.ProposalWorkflowStepInstance(
                    proposal=proposal,
                    step=step_def.id,
                    status=(
                        WorkflowStepInstanceStatuses.PENDING
                        if step_def.id in enabled_step_ids
                        else WorkflowStepInstanceStatuses.SKIPPED
                    ),
                )
                for step_def in WORKFLOW_STEPS
            ]
            models.ProposalWorkflowStepInstance.objects.bulk_create(instances_to_create)

            if first_step_id:
                first_step = models.ProposalWorkflowStepInstance.objects.get(
                    proposal=proposal, step=first_step_id
                )
                first_step.status = WorkflowStepInstanceStatuses.ACTIVE
                first_step.started_at = timezone.now()
                call_step = next(
                    (s for s in enabled_steps if s.step == first_step_id), None
                )
                if call_step and call_step.duration_in_days:
                    first_step.deadline = first_step.started_at + timedelta(
                        days=call_step.duration_in_days
                    )
                first_step.save(update_fields=["status", "started_at", "deadline"])
                proposal.state = ProposalStates.IN_REVIEW
                proposal.workflow_step = first_step_id
            else:
                proposal.state = ProposalStates.SUBMITTED

            proposal.save()

        tasks.notify_user_about_proposal_state_update.delay(
            proposal.uuid, previous_state, proposal.state
        )
        tasks.notify_call_managers_about_new_proposal_submission.delay(proposal.uuid)
        return response.Response(
            "Proposal has been submitted.",
            status=status.HTTP_200_OK,
        )

    submit_validators = [core_validators.StateValidator(ProposalStates.DRAFT)]

    submit_permissions = [is_creator]

    def perform_create(self, serializer):
        # Validate user eligibility against call restrictions before creating proposal
        round_obj = serializer.validated_data.get("round")
        if round_obj:
            call = round_obj.call
            permissions_utils.validate_user_restrictions(call, self.request.user)

        proposal: models.Proposal = serializer.save()
        proposal.add_user(
            self.request.user,
            ProposalRole.MANAGER,
            created_by=self.request.user,
        )

    @extend_schema(
        methods=["get"],
        operation_id="proposal_proposals_resources_list",
        request=None,
        responses=serializers.RequestedResourceSerializer(many=True),
        description="List resources for a proposal.",
        filters=False,
    )
    @extend_schema(
        methods=["post"],
        operation_id="proposal_proposals_resources_set",
        request=serializers.RequestedResourceSerializer,
        responses=serializers.RequestedResourceSerializer,
        description="Create resource for a proposal.",
    )
    @decorators.action(detail=True, methods=["get", "post"])
    def resources(self, request, uuid=None):
        return self.action_list_method("requestedresource_set")(self, request, uuid)

    resources_serializer_class = serializers.RequestedResourceSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RequestedResourceSerializer}
    )
    def resource_detail(self, request, uuid=None, obj_uuid=None):
        def validate_proposal_state(requested_resource):
            if requested_resource.proposal.state != ProposalStates.DRAFT:
                raise IncorrectStateException(
                    "Only proposals with a draft status are available for editing."
                )

        return self.action_detail_method(
            "requestedresource_set",
            delete_validators=[validate_proposal_state],
            update_validators=[validate_proposal_state],
        )(self, request, uuid, obj_uuid)

    resource_detail_serializer_class = serializers.RequestedResourceSerializer

    @extend_schema(
        description="Attach document to proposal.",
        request=serializers.ProposalDocumentationSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_document(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        serializer = self.get_serializer(
            context=self.get_serializer_context(),
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(proposal=proposal)

        event_logger.emit(
            f"Attachment for proposal {proposal.name} has been added.",
            event_type=EventType.PROPOSAL_DOCUMENT_ADDED,
            event_context={"proposal": proposal},
            scopes=[_get_customer(proposal)],
        )
        return response.Response(status=status.HTTP_200_OK)

    attach_document_serializer_class = serializers.ProposalDocumentationSerializer

    # Workflow Step Endpoints

    @extend_schema(
        description="List all workflow step instances for this proposal.",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.ProposalWorkflowStepInstanceSerializer(
                many=True
            )
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def workflow_states(self, request, uuid=None):
        # Access control is the viewset-level queryset filter
        # (filter_queryset_for_user): unrelated users get an empty queryset
        # and self.get_object() returns 404 before any of this code runs.
        # Applicants, project members, and call team all reach the same
        # response shape — the per-field visibility (internal_notes) is
        # gated below via the serializer context.
        proposal = self.get_object()
        instances = proposal.workflow_step_instances.all()
        # Pre-load step configs once (max ~6 rows per proposal) so the
        # serializer's derived fields don't trigger a per-row query for
        # applicant_visible / duration_in_days / responsible_role. Use
        # ``call_id`` rather than ``call`` so we don't fetch the Call row
        # we never read fields from.
        step_configs_by_key = {
            cs.step: cs
            for cs in models.CallWorkflowStep.objects.filter(
                call_id=proposal.round.call_id
            ).only("step", "applicant_visible", "duration_in_days", "responsible_role")
        }
        can_view_internal_notes = proposal_permissions.user_can_view_internal_notes(
            request.user, proposal
        )
        serializer = serializers.ProposalWorkflowStepInstanceSerializer(
            instances,
            many=True,
            context={
                "step_configs_by_key": step_configs_by_key,
                "can_view_internal_notes": can_view_internal_notes,
                # completed_by can reveal reviewer / panel-member identity;
                # only the call team (same gate as internal notes) or calls
                # that reveal reviewer identity to submitters may see it.
                "can_view_step_actors": (
                    can_view_internal_notes
                    or proposal.round.call.reviewer_identity_visible_to_submitters
                ),
                # outcome / outcome_reason on the peer-review steps are review
                # content, gated by reviews_visible_to_submitters.
                "can_view_review_content": (
                    can_view_internal_notes
                    or proposal.round.call.reviews_visible_to_submitters
                ),
            },
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    workflow_states_serializer_class = (
        serializers.ProposalWorkflowStepInstanceSerializer
    )

    @extend_schema(
        description="Complete the current workflow step with an outcome.",
        request=serializers.CompleteWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.CompleteWorkflowStepResponseSerializer
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def complete_workflow_step(self, request, uuid=None):
        proposal = self.get_object()

        if proposal.state != ProposalStates.IN_REVIEW:
            return response.Response(
                {"detail": "Proposal must be in review state."},
                status=status.HTTP_409_CONFLICT,
            )

        if not proposal.workflow_step:
            return response.Response(
                {"detail": "Proposal has no active workflow step."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Pass active_step in context so the serializer can validate the
        # outcome against the per-step allow-list.
        input_serializer = serializers.CompleteWorkflowStepSerializer(
            data=request.data,
            context={"active_step": proposal.workflow_step},
        )
        input_serializer.is_valid(raise_exception=True)

        outcome = input_serializer.validated_data["outcome"]
        client_step_uuid = input_serializer.validated_data["step_uuid"]
        outcome_reason = input_serializer.validated_data.get("outcome_reason", "")
        # Drop internal_notes for callers who can't read it back. The
        # responsible-role check on this action lets an applicant act on
        # applicant-owned steps (e.g. award_response); without this guard
        # they could silently inject text into a field the call-management
        # team treats as internal.
        internal_notes = (
            input_serializer.validated_data.get("internal_notes", "")
            if proposal_permissions.user_can_view_internal_notes(request.user, proposal)
            else ""
        )

        with transaction.atomic():
            # Lock the proposal row first so complete/reject/advance serialise
            # on the same resource, in a consistent lock order (proposal then
            # step instance) to avoid deadlocks.
            models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            # Lock the active step row to serialise concurrent completions.
            current_instance = (
                proposal.workflow_step_instances.select_for_update()
                .filter(
                    step=proposal.workflow_step,
                    status=WorkflowStepInstanceStatuses.ACTIVE,
                    uuid=client_step_uuid,
                )
                .first()
            )
            if current_instance is None:
                return response.Response(
                    {"detail": "Workflow step has changed; refresh and retry."},
                    status=status.HTTP_409_CONFLICT,
                )

            try:
                next_instance = workflow_service.complete_step(
                    proposal=proposal,
                    current_instance=current_instance,
                    outcome=outcome,
                    outcome_reason=outcome_reason,
                    completed_by=request.user,
                    internal_notes=internal_notes,
                )
            except ValueError as e:
                # Step gate not satisfied (e.g. too few reviews / score below
                # threshold). Roll back and surface the reason to the caller.
                raise exceptions.ValidationError({"detail": str(e)})
            # Step-activation notifications were intentionally removed; see
            # commit d2d5fb77c "Drop step-activation notifications [WAL-9346]".
            # Re-introducing them requires a debounce/digest policy first.

        if next_instance is None:
            if workflow_service.is_awaiting_manual_advance(proposal):
                response_serializer = (
                    serializers.CompleteWorkflowStepResponseSerializer(
                        {"detail": "Step completed. Awaiting manual advance."}
                    )
                )
                return response.Response(
                    response_serializer.data, status=status.HTTP_200_OK
                )
            # Terminal step reached: complete_step set the final state — accepted
            # (and provisioned), or rejected / canceled on a negative outcome.
            # Notify with the actual outcome so a rejected/canceled terminal
            # never sends an "accepted" email.
            proposal.refresh_from_db()
            tasks.notify_proposal_decision(
                proposal.uuid, ProposalStates.IN_REVIEW, proposal.state
            )
            response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
                {
                    "detail": _terminal_workflow_detail(proposal.state),
                    "proposal_state": proposal.state,
                }
            )
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )

        next_step_def = next(
            (s for s in WORKFLOW_STEPS if s.id == next_instance.step), None
        )
        response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
            {
                "detail": f"Step completed. Advanced to: {next_step_def.name if next_step_def else next_instance.step}",
                "next_step": next_instance.step,
            }
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    complete_workflow_step_serializer_class = serializers.CompleteWorkflowStepSerializer
    complete_workflow_step_permissions = [
        proposal_permissions.can_act_on_active_workflow_step
    ]

    @extend_schema(
        description="Reject the proposal at the current workflow step.",
        request=serializers.RejectWorkflowStepSerializer,
        responses={
            status.HTTP_200_OK: serializers.RejectWorkflowStepResponseSerializer
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def reject_workflow_step(self, request, uuid=None):
        proposal = self.get_object()
        input_serializer = serializers.RejectWorkflowStepSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        if proposal.state != ProposalStates.IN_REVIEW:
            return response.Response(
                {"detail": "Proposal must be in review state."},
                status=status.HTTP_409_CONFLICT,
            )

        client_step_uuid = input_serializer.validated_data["step_uuid"]
        reason = input_serializer.validated_data["reason"]
        # See complete_workflow_step for the symmetric write-side gate: only
        # call-management-team users may persist internal_notes; applicants
        # acting on their own step would otherwise leak private text.
        internal_notes = (
            input_serializer.validated_data.get("internal_notes", "")
            if proposal_permissions.user_can_view_internal_notes(request.user, proposal)
            else ""
        )

        with transaction.atomic():
            # Lock the proposal row first (consistent order with
            # complete/advance) to serialise concurrent workflow mutations.
            models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            current_instance = (
                proposal.workflow_step_instances.select_for_update()
                .filter(
                    step=proposal.workflow_step,
                    status=WorkflowStepInstanceStatuses.ACTIVE,
                    uuid=client_step_uuid,
                )
                .first()
            )
            if current_instance is None:
                return response.Response(
                    {"detail": "Workflow step has changed; refresh and retry."},
                    status=status.HTTP_409_CONFLICT,
                )

            workflow_service.reject_at_step(
                proposal=proposal,
                current_instance=current_instance,
                reason=reason,
                completed_by=request.user,
                internal_notes=internal_notes,
            )

        response_serializer = serializers.RejectWorkflowStepResponseSerializer(
            {"detail": "Proposal rejected.", "proposal_state": ProposalStates.REJECTED}
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    reject_workflow_step_serializer_class = serializers.RejectWorkflowStepSerializer
    reject_workflow_step_permissions = [
        proposal_permissions.can_act_on_active_workflow_step
    ]

    @extend_schema(
        description=(
            "Manually advance a workflow that is awaiting call-manager confirmation."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.CompleteWorkflowStepResponseSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def advance_workflow_step(self, request, uuid=None):
        proposal = self.get_object()

        if proposal.state != ProposalStates.IN_REVIEW:
            return response.Response(
                {"detail": "Proposal must be in review state."},
                status=status.HTTP_409_CONFLICT,
            )

        from_step = proposal.workflow_step
        actor = request.user.full_name or request.user.username

        with transaction.atomic():
            # Lock the proposal row to serialise concurrent advances.
            locked = models.Proposal.objects.select_for_update().get(pk=proposal.pk)
            # Re-check state under the lock: a concurrent transition could
            # have moved the proposal out of IN_REVIEW between the pre-lock
            # read above and the lock acquisition here.
            if locked.state != ProposalStates.IN_REVIEW:
                return response.Response(
                    {"detail": "Proposal must be in review state."},
                    status=status.HTTP_409_CONFLICT,
                )
            try:
                next_instance = workflow_service.advance_step(
                    proposal=locked, acting_user=request.user
                )
            except ValueError as e:
                return response.Response(
                    {"detail": str(e)}, status=status.HTTP_409_CONFLICT
                )

            target = (
                "workflow completion" if next_instance is None else next_instance.step
            )
            # Pass user-controlled strings as named placeholders in
            # event_context rather than f-string interpolation. The emit
            # helper calls .format(**context) on the template, so an f-string
            # that already contains a name like "{user_token_lifetime}" would
            # otherwise resolve against the event context at emit time.
            event_logger.emit(
                "Proposal {proposal_name} workflow manually advanced "
                "from {from_step} to {target} by {actor}.",
                event_type=EventType.PROPOSAL_WORKFLOW_ADVANCED,
                event_context={
                    "proposal": locked,
                    "from_step": from_step,
                    "target": target,
                    "actor": actor,
                },
                scopes=[_get_customer(locked)],
            )

        if next_instance is None:
            # Manual advance reached the terminal step: accepted + provisioned
            # inside advance_step. Fire the shared decision notifications.
            tasks.notify_proposal_decision(
                proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
            )
            response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
                {
                    "detail": "Workflow completed. Proposal accepted.",
                    "proposal_state": ProposalStates.ACCEPTED,
                }
            )
            return response.Response(
                response_serializer.data, status=status.HTTP_200_OK
            )

        next_step_def = next(
            (s for s in WORKFLOW_STEPS if s.id == next_instance.step), None
        )
        response_serializer = serializers.CompleteWorkflowStepResponseSerializer(
            {
                "detail": f"Advanced to: {next_step_def.name if next_step_def else next_instance.step}",
                "next_step": next_instance.step,
            }
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    advance_workflow_step_permissions = [proposal_permissions.can_advance_workflow_step]

    @extend_schema(
        request=serializers.ProposalDetachDocumentsSerializer,
        responses=None,
        description="Detach documents from proposal.",
    )
    @decorators.action(detail=True, methods=["post"])
    def detach_documents(self, request, uuid=None):
        proposal = cast(models.Proposal, self.get_object())
        # Handle both JSON (list) and form data (QueryDict with getlist)
        if hasattr(request.data, "getlist"):
            documents = request.data.getlist("documents", [])
        else:
            documents = request.data.get("documents", [])
        for doc_uuid in documents:
            try:
                doc = models.ProposalDocumentation.objects.get(
                    proposal=proposal,
                    uuid=doc_uuid,
                )
                doc.delete()
                event_logger.emit(
                    f"Attachment for proposal {proposal.name} has been removed.",
                    event_type=EventType.PROPOSAL_DOCUMENT_REMOVED,
                    event_context={"proposal": proposal},
                    scopes=[_get_customer(proposal)],
                )
                logger.info(f"Attachment for {proposal.name} has been removed.")
            except models.ProposalDocumentation.DoesNotExist:
                pass  # Skip non-existent documents

        return response.Response(
            "Documents removed successfully",
            status=status.HTTP_200_OK,
        )

    detach_documents_serializer_class = serializers.ProposalDetachDocumentsSerializer

    # NOTE: the legacy one-click `approve`/`reject` actions were removed — every
    # proposal is now driven through the workflow engine (complete/advance/reject
    # workflow-step actions), which is the single provisioning path. See the
    # data migration that backfilled any pre-engine `submitted`/`in_review`
    # proposals with workflow instances.

    # Checklist Integration Endpoints
    # Checklist methods are now provided by ChecklistViewSetMixin
    # - checklist: Get checklist with questions and existing answers
    # - submit_answers: Submit checklist answers (overridden below)
    # - completion_status: Get completion status

    @extend_schema(
        description="Submit checklist answers.",
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={
            200: serializers.ProposalChecklistAnswerSubmitResponseSerializer,
            400: {"description": "Validation error or no checklist configured"},
            404: {"description": "Object not found"},
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def submit_answers(self, request, uuid=None):
        """Submit checklist answers with proposal-specific response that includes review status."""
        obj = self.get_object()

        completion = self.get_checklist_completion(obj)
        if not completion:
            return response.Response(
                {"detail": "No checklist configured for this object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate input data
        submit_serializer = checklist_serializers.AnswerSubmitSerializer(
            data=request.data,
            many=True,
            context={"completion": completion, "request": request},
        )
        submit_serializer.is_valid(raise_exception=True)

        # Process each answer
        for answer_data in submit_serializer.validated_data:
            question = answer_data["question"]
            answer_value = answer_data["answer_data"]

            if answer_value is None:
                # Remove answer (hard delete)
                checklist_models.Answer.objects.filter(
                    completion=completion,
                    question=question,
                    user=request.user,
                ).delete()
            else:
                # Create or update answer using direct foreign key
                checklist_models.Answer.objects.update_or_create(
                    completion=completion,
                    question=question,
                    user=request.user,
                    defaults={"answer_data": answer_value},
                )

        # Update completion status to reflect any changes from additions/removals
        completion.update_completion_status()

        # Return updated completion status
        completion.refresh_from_db()

        # Create response data with proposal-specific serializer that includes review status
        response_data = {
            "detail": "Answers submitted successfully",
            "completion": completion,
        }

        response_serializer = (
            serializers.ProposalChecklistAnswerSubmitResponseSerializer(
                response_data, context={"request": request}
            )
        )

        return response.Response(response_serializer.data)


class ReviewViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalReviewSerializer
    filterset_class = filters.ReviewFilter
    queryset = models.Review.objects.all()

    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW)
    ]

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.Review.objects.all().order_by("created")

        # Base queries for authorized users (call organizers, call managers, reviewers)
        authorized_query = (
            Q(
                proposal__round__call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
            | Q(
                proposal__round__call__manager__customer__in=get_connected_customers(
                    user
                )
            )
            | Q(proposal__round__call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(reviewer=user)
        )

        # For proposal submitters - apply visibility controls
        submitter_query = Q(proposal__created_by=user)

        # Key change: Only include reviews for submitters if reviews are visible in the call
        submitter_query &= Q(proposal__round__call__reviews_visible_to_submitters=True)

        # Only show submitted reviews to submitters (existing logic)
        submitter_query &= Q(state=models.Review.States.SUBMITTED)

        # For submitters, reviews are visible only if the proposal has a decision state
        submitter_query &= Q(
            proposal__state__in=[
                models.Proposal.States.ACCEPTED,
                models.Proposal.States.REJECTED,
            ]
        )

        return models.Review.objects.filter(
            authorized_query | submitter_query
        ).order_by("created")

    def perform_create(self, serializer):
        proposal = serializer.validated_data["proposal"]
        if proposal.state not in [
            ProposalStates.DRAFT,
            ProposalStates.IN_REVIEW,
            ProposalStates.SUBMITTED,
        ]:
            raise exceptions.ValidationError(
                _("Valid states for proposals: Draft, In Review, Submitted.")
            )
        review: models.Review = serializer.save()
        tasks.notify_reviewer_about_assignment.delay(review.uuid)

    def check_create_permissions(request, view, obj=None):
        """Check permissions for creating reviews."""
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal = serializer.validated_data["proposal"]

        user = request.user
        permission = PermissionEnum.MANAGE_PROPOSAL_REVIEW
        call = proposal.round.call

        if not (
            has_permission(user, permission, call)
            or has_permission(user, permission, call.manager)
        ):
            raise exceptions.PermissionDenied()

    def check_destroy_permissions(request, view, obj=None):
        """Check permissions for destroying reviews."""
        if obj and not (
            has_permission(
                request.user,
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                obj.proposal.round.call,
            )
            or has_permission(
                request.user,
                PermissionEnum.MANAGE_PROPOSAL_REVIEW,
                obj.proposal.round.call.manager,
            )
        ):
            raise exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [check_destroy_permissions]

    def action_permission_check(request, view, obj: models.Review | None = None):
        if not obj:
            return

        user = request.user

        if user.is_staff or obj.reviewer == user:
            return

        raise exceptions.PermissionDenied()

    @extend_schema(
        description="Reject a review, changing its state to REJECTED.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        review: models.Review = self.get_object()
        review.state = models.Review.States.REJECTED
        review.save()
        tasks.notify_call_managers_about_rejected_review.delay(review.uuid)
        return response.Response(
            "Review has been rejected.",
            status=status.HTTP_200_OK,
        )

    reject_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW),
    ]

    @extend_schema(
        description="Submit a review, changing its state to SUBMITTED.",
        request=ReviewSubmitSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        review: models.Review = self.get_object()
        serializer = ReviewSubmitSerializer(
            instance=review, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(state=models.Review.States.SUBMITTED)
        tasks.notify_call_managers_about_new_review.delay(review.uuid)
        tasks.notify_manager_when_reviews_are_completed.delay(review.proposal.uuid)
        return response.Response(
            "Review has been submitted.",
            status=status.HTTP_200_OK,
        )

    submit_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW),
    ]
    accept_permissions = reject_permissions = submit_permissions = (
        update_permissions
    ) = partial_update_permissions = [action_permission_check]


class ProviderRequestedOfferingViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    queryset = models.RequestedOffering.objects.filter().order_by(
        "offering", "call", "created"
    )
    serializer_class = serializers.ProviderRequestedOfferingSerializer
    filterset_class = filters.RequestedOfferingFilter
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)

    @extend_schema(
        description="Accept a requested offering.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        requested_offering.state = RequestedOfferingStates.ACCEPTED
        requested_offering.approved_by = self.request.user
        requested_offering.save()
        tasks.notify_offering_request_decision.delay(requested_offering.uuid)
        return response.Response(
            "The request on offering has been accepted.",
            status=status.HTTP_200_OK,
        )

    accept_validators = [
        core_validators.StateValidator(RequestedOfferingStates.REQUESTED)
    ]

    @extend_schema(
        description="Cancel a requested offering.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        requested_offering: models.RequestedOffering = self.get_object()
        requested_offering.state = RequestedOfferingStates.CANCELED
        requested_offering.approved_by = self.request.user
        requested_offering.save()
        tasks.notify_offering_request_decision.delay(requested_offering.uuid)
        return response.Response(
            "The request on offering has been canceled.",
            status=status.HTTP_200_OK,
        )

    cancel_validators = [
        core_validators.StateValidator(RequestedOfferingStates.REQUESTED)
    ]

    accept_permissions = cancel_permissions = [
        proposal_permissions.user_can_accept_requested_offering
    ]


class ProviderRequestedResourceViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProviderRequestedResourceSerializer
    filterset_class = filters.RequestedResourceFilter
    filter_backends = (DjangoFilterBackend,)

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.RequestedResource.objects.all().order_by(
                "resource", "proposal", "created"
            )

        offerings_ids = (
            marketplace_models.Offering.objects.all()
            .filter_for_user(user)
            .values_list("id", flat=True)
        )
        return models.RequestedResource.objects.filter(
            requested_offering__offering_id__in=offerings_ids
        ).order_by("resource", "proposal", "created")


class RoundViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.CallRoundSerializer
    filterset_class = []
    filter_backends = (DjangoFilterBackend,)

    def get_queryset(self):
        return filter_queryset_for_user(models.Round.objects.all(), self.request.user)

    @extend_schema(
        description="Return list of reviewers for round.",
        request=None,
        responses=serializers.RoundReviewerSerializer(many=True),
    )
    @decorators.action(detail=True)
    def reviewers(self, request, uuid=None):
        round_obj = self.get_object()

        unique_reviewer_ids = (
            models.Review.objects.filter(proposal__round=round_obj)
            .values_list("reviewer", flat=True)
            .distinct()
        )
        users = User.objects.filter(id__in=unique_reviewer_ids)

        proposals = models.Proposal.objects.filter(
            review__reviewer=OuterRef("pk"), round=round_obj
        )

        accepted_proposals_subquery = proposals.filter(
            state=ProposalStates.ACCEPTED
        ).values("pk")

        rejected_proposals_subquery = proposals.filter(
            state=ProposalStates.REJECTED
        ).values("pk")

        in_review_proposals_subquery = proposals.filter(
            state=ProposalStates.IN_REVIEW
        ).values("pk")

        users = users.annotate(
            accepted_proposals=Coalesce(SubqueryCount(accepted_proposals_subquery), 0),
            rejected_proposals=Coalesce(SubqueryCount(rejected_proposals_subquery), 0),
            in_review_proposals=Coalesce(
                SubqueryCount(in_review_proposals_subquery), 0
            ),
        )

        page = self.paginate_queryset(users)
        serializer = serializers.RoundReviewerSerializer(
            page, many=True, context={"round_obj": round_obj}
        )
        return self.get_paginated_response(serializer.data)


class ProposalProjectRoleMappingViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalProjectRoleMappingSerializer
    queryset = models.ProposalProjectRoleMapping.objects.all().order_by("call")
    filterset_class = filters.ProposalProjectRoleMappingFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [proposal_permissions.CanUpdateCallPermission]


# =============================================================================
# Reviewer Profile ViewSets
# =============================================================================


class ExpertiseCategoryViewSet(ReadOnlyActionsViewSet):
    """Read-only ViewSet for expertise categories (taxonomy)."""

    lookup_field = "uuid"
    queryset = models.ExpertiseCategory.objects.all().order_by("code")
    serializer_class = serializers.ExpertiseCategorySerializer
    filterset_class = filters.ExpertiseCategoryFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = (rf_permissions.IsAuthenticated,)


def check_reviewer_profile_update_permission(request, view, obj=None):
    """Check if user can update a reviewer profile."""
    if request.user.is_staff:
        return
    if obj and obj.user == request.user:
        return
    raise exceptions.PermissionDenied(
        _("You do not have permission to update this reviewer profile.")
    )


class ReviewerProfileViewSet(ActionsViewSet):
    """ViewSet for managing reviewer profiles."""

    lookup_field = "uuid"
    queryset = models.ReviewerProfile.objects.all().order_by(
        "user__first_name", "user__last_name"
    )
    serializer_class = serializers.ReviewerProfileSerializer
    filterset_class = filters.ReviewerProfileFilter
    filter_backends = (DjangoFilterBackend,)
    update_permissions = partial_update_permissions = destroy_permissions = [
        check_reviewer_profile_update_permission
    ]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.ReviewerProfile.objects.all().order_by(
                "user__first_name", "user__last_name"
            )
        # Users can see their own profile and profiles of ACCEPTED pool members
        # (pending invitations don't expose profiles to managers)
        return (
            models.ReviewerProfile.objects.filter(
                Q(user=user)  # Own profile always visible
                | Q(
                    # Only ACCEPTED pool members visible to managers
                    pool_memberships__call__in=get_connected_calls(
                        user, CallRole.MANAGER
                    ),
                    pool_memberships__invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
                )
            )
            .distinct()
            .order_by("user__first_name", "user__last_name")
        )

    @extend_schema(
        description="Get or create reviewer profile for the current user.",
        request=serializers.ReviewerProfileCreateSerializer,
        responses=serializers.ReviewerProfileSerializer,
    )
    @decorators.action(detail=False, methods=["get", "post", "patch"])
    def me(self, request):
        """Get or create reviewer profile for the current user."""
        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            if request.method == "GET":
                return response.Response(
                    {"detail": "Reviewer profile not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            profile = None

        if request.method == "GET":
            serializer = self.get_serializer(profile)
            return response.Response(serializer.data)
        elif request.method == "POST" and profile is None:
            serializer = serializers.ReviewerProfileCreateSerializer(
                data=request.data, context=self.get_serializer_context()
            )
            serializer.is_valid(raise_exception=True)
            profile = serializer.save()
            # Create stats record
            models.ReviewerStats.objects.create(reviewer_profile=profile)
            return response.Response(
                self.get_serializer(profile).data,
                status=status.HTTP_201_CREATED,
            )
        elif request.method in ["POST", "PATCH"] and profile is not None:
            serializer = serializers.ReviewerProfileSerializer(
                profile,
                data=request.data,
                partial=True,
                context=self.get_serializer_context(),
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return response.Response(serializer.data)
        else:
            return response.Response(
                {"detail": "Profile already exists. Use PATCH to update."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Note: Nested affiliations, expertise, and publications management
    # are handled by dedicated ViewSets (ReviewerProfileAffiliationViewSet,
    # ReviewerProfileExpertiseViewSet, ReviewerProfilePublicationViewSet)
    # registered in waldur_core/server/urls.py using NestedSimpleRouter.
    # These provide full CRUD operations at endpoints like:
    # /api/reviewer-profiles/{uuid}/affiliations/
    # /api/reviewer-profiles/{uuid}/expertise/
    # /api/reviewer-profiles/{uuid}/publications/

    # ORCID Integration
    @extend_schema(
        description="Get ORCID OAuth authorization URL.",
        responses={
            200: {
                "type": "object",
                "properties": {"authorization_url": {"type": "string"}},
            }
        },
    )
    @decorators.action(detail=True, methods=["get"], url_path="connect-orcid")
    def connect_orcid(self, request, uuid=None):
        """Get ORCID OAuth authorization URL to initiate connection."""
        profile = self.get_object()

        # Check if user owns this profile
        if profile.user != request.user and not request.user.is_staff:
            raise exceptions.PermissionDenied(
                _("You can only connect ORCID to your own profile.")
            )

        if not orcid_service.is_orcid_configured():
            raise exceptions.ValidationError(_("ORCID integration is not configured."))

        # Generate state token for CSRF protection
        state = secrets.token_urlsafe(32)
        # Store state in session or cache for validation
        request.session[f"orcid_state_{profile.uuid}"] = state

        auth_url = orcid_service.get_authorization_url(state=state)
        return response.Response({"authorization_url": auth_url})

    @extend_schema(
        description="Complete ORCID OAuth connection with authorization code.",
        request=serializers.OrcidCallbackSerializer,
        responses=serializers.ReviewerProfileSerializer,
    )
    @decorators.action(detail=True, methods=["post"], url_path="connect-orcid/callback")
    def connect_orcid_callback(self, request, uuid=None):
        """Complete ORCID OAuth flow with authorization code."""
        profile = self.get_object()

        if profile.user != request.user and not request.user.is_staff:
            raise exceptions.PermissionDenied(
                _("You can only connect ORCID to your own profile.")
            )

        code = request.data.get("code")
        if not code:
            raise exceptions.ValidationError(
                {"code": _("Authorization code is required.")}
            )

        try:
            token_data = orcid_service.exchange_code_for_token(code)
        except orcid_service.ORCIDAuthError as e:
            raise exceptions.ValidationError({"code": str(e)})

        # Update profile with ORCID data
        profile.orcid_id = token_data.get("orcid")
        profile.orcid_access_token = token_data.get("access_token", "")
        profile.orcid_refresh_token = token_data.get("refresh_token", "")
        profile.orcid_last_sync = timezone.now()
        profile.save()

        return response.Response(self.get_serializer(profile).data)

    @extend_schema(
        description="Sync profile data from ORCID.",
        responses={200: serializers.OrcidSyncResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="sync-orcid")
    def sync_orcid(self, request, uuid=None):
        """Sync profile data from connected ORCID account."""
        profile = self.get_object()

        if profile.user != request.user and not request.user.is_staff:
            raise exceptions.PermissionDenied(
                _("You can only sync your own ORCID profile.")
            )

        if not profile.orcid_id:
            raise exceptions.ValidationError(
                _("ORCID is not connected. Please connect ORCID first.")
            )

        try:
            # Import publications
            pub_result = orcid_service.import_orcid_works(profile)
            # Import affiliations
            affil_result = orcid_service.import_orcid_affiliations(profile)
            # Import keywords as expertise
            keyword_result = orcid_service.import_orcid_keywords(profile)

            profile.orcid_last_sync = timezone.now()
            profile.save(update_fields=["orcid_last_sync"])

            return response.Response(
                {
                    "imported": {
                        "publications": pub_result,
                        "affiliations": affil_result,
                        "keywords": keyword_result,
                    },
                    "last_sync": profile.orcid_last_sync,
                }
            )
        except orcid_service.ORCIDAPIError as e:
            raise exceptions.ValidationError({"detail": str(e)})

    @extend_schema(
        description="Disconnect ORCID from profile.",
        responses={200: serializers.OrcidDisconnectResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="disconnect-orcid")
    def disconnect_orcid(self, request, uuid=None):
        """Disconnect ORCID from profile."""
        profile = self.get_object()

        if profile.user != request.user and not request.user.is_staff:
            raise exceptions.PermissionDenied(
                _("You can only disconnect your own ORCID.")
            )

        profile.orcid_id = ""
        profile.orcid_access_token = ""
        profile.orcid_refresh_token = ""
        profile.orcid_last_sync = None
        profile.save(
            update_fields=[
                "orcid_id",
                "orcid_access_token",
                "orcid_refresh_token",
                "orcid_last_sync",
            ]
        )

        return response.Response({"detail": _("ORCID disconnected successfully.")})

    @extend_schema(
        description="Import publications from ORCID or other sources.",
        request=serializers.ImportPublicationsSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {"imported_count": {"type": "integer"}},
            }
        },
    )
    @decorators.action(detail=True, methods=["post"], url_path="import-publications")
    def import_publications(self, request, uuid=None):
        """Import publications from various sources."""
        profile = self.get_object()

        if profile.user != request.user and not request.user.is_staff:
            raise exceptions.PermissionDenied(
                _("You can only import publications to your own profile.")
            )

        source = request.data.get("source", "orcid")

        if source == "orcid":
            if not profile.orcid_id:
                raise exceptions.ValidationError(
                    _("ORCID is not connected. Please connect ORCID first.")
                )

            result = orcid_service.import_orcid_works(profile)
            return response.Response({"imported_count": result.get("created", 0)})

        elif source == "doi":
            doi = request.data.get("doi")
            if not doi:
                raise exceptions.ValidationError({"doi": _("DOI is required.")})

            try:
                pub_data = orcid_service.fetch_publication_by_doi(doi)
                if pub_data:
                    pub, created = models.ReviewerPublication.objects.get_or_create(
                        reviewer_profile=profile,
                        doi=doi,
                        defaults=pub_data,
                    )
                    return response.Response(
                        {
                            "imported_count": 1 if created else 0,
                            "publication": serializers.ReviewerPublicationSerializer(
                                pub
                            ).data,
                        }
                    )
                else:
                    raise exceptions.ValidationError(
                        {"doi": _("Could not find publication with this DOI.")}
                    )
            except Exception as e:
                raise exceptions.ValidationError({"detail": str(e)})

        else:
            raise exceptions.ValidationError(
                {"source": _("Invalid source. Use 'orcid' or 'doi'.")}
            )

    # Profile visibility management
    @extend_schema(
        description=(
            "Publish reviewer profile for discovery by call managers. "
            "Warning: Publishing makes your full profile visible to call managers globally."
        ),
        responses={
            200: {
                "type": "object",
                "properties": {
                    "is_published": {"type": "boolean"},
                    "published_at": {"type": "string", "format": "date-time"},
                    "warning": {"type": "string"},
                },
            }
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def publish(self, request):
        """Publish reviewer profile for discovery."""
        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must create a reviewer profile first.")
            )

        if profile.is_published:
            return response.Response(
                {
                    "is_published": True,
                    "published_at": profile.published_at,
                    "detail": _("Profile is already published."),
                }
            )

        profile.is_published = True
        profile.published_at = timezone.now()
        profile.save(update_fields=["is_published", "published_at"])

        return response.Response(
            {
                "is_published": True,
                "published_at": profile.published_at,
                "warning": _(
                    "Your full reviewer profile is now discoverable by call managers globally. "
                    "This includes your biography, publications, affiliations, and expertise areas."
                ),
            }
        )

    @extend_schema(
        description="Unpublish reviewer profile to remove it from discovery.",
        responses={
            200: {
                "type": "object",
                "properties": {
                    "is_published": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
            }
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def unpublish(self, request):
        """Unpublish reviewer profile to hide it from discovery."""
        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must create a reviewer profile first.")
            )

        if not profile.is_published:
            return response.Response(
                {
                    "is_published": False,
                    "detail": _("Profile is already unpublished."),
                }
            )

        profile.is_published = False
        profile.save(update_fields=["is_published"])

        return response.Response(
            {
                "is_published": False,
                "detail": _(
                    "Your profile is no longer discoverable. "
                    "Existing invitations and pool memberships are not affected."
                ),
            }
        )


# =============================================================================
# COI (Conflict of Interest) ViewSets
# =============================================================================


class ConflictOfInterestViewSet(ActionsViewSet):
    """ViewSet for managing conflicts of interest."""

    lookup_field = "uuid"
    queryset = models.ConflictOfInterest.objects.all().order_by("-detected_at")
    serializer_class = serializers.ConflictOfInterestSerializer
    filterset_class = filters.ConflictOfInterestFilter
    filter_backends = (DjangoFilterBackend,)
    disabled_actions = ["create", "destroy"]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.ConflictOfInterest.objects.all().order_by("-detected_at")
        # Call managers can see COIs for their calls
        return models.ConflictOfInterest.objects.filter(
            Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
            | Q(reviewer__user=user)  # Reviewers can see their own COIs
        ).order_by("-detected_at")

    @extend_schema(
        description="Dismiss a conflict of interest (not a real conflict).",
        request=serializers.COIStatusUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ConflictOfInterestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def dismiss(self, request, uuid=None):
        """Dismiss a detected conflict (mark as not a real conflict)."""
        coi = self.get_object()
        serializer = serializers.COIStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        coi.status = "dismissed"
        coi.reviewed_by = request.user
        coi.reviewed_at = timezone.now()
        coi.review_notes = serializer.validated_data.get("review_notes", "")
        coi.save()

        self._unblock_related_assignments(coi)

        return response.Response(
            self.get_serializer(coi).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Waive a conflict with a management plan.",
        request=serializers.COIStatusUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ConflictOfInterestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def waive(self, request, uuid=None):
        """Waive a conflict with a management plan."""
        coi = self.get_object()
        serializer = serializers.COIStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not serializer.validated_data.get("management_plan"):
            raise exceptions.ValidationError(
                {"management_plan": _("Management plan is required when waiving.")}
            )

        coi.status = "waived"
        coi.reviewed_by = request.user
        coi.reviewed_at = timezone.now()
        coi.review_notes = serializer.validated_data.get("review_notes", "")
        coi.management_plan = serializer.validated_data["management_plan"]
        coi.save()

        self._unblock_related_assignments(coi)

        return response.Response(
            self.get_serializer(coi).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Recuse reviewer from the proposal.",
        request=serializers.COIStatusUpdateSerializer,
        responses={status.HTTP_200_OK: serializers.ConflictOfInterestSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def recuse(self, request, uuid=None):
        """Recuse the reviewer from the proposal."""
        coi = self.get_object()
        serializer = serializers.COIStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        coi.status = "recused"
        coi.reviewed_by = request.user
        coi.reviewed_at = timezone.now()
        coi.review_notes = serializer.validated_data.get("review_notes", "")
        coi.save()

        # Clean up any existing review assignments for this reviewer-proposal pair
        self._cleanup_reviewer_assignments(coi)

        return response.Response(
            self.get_serializer(coi).data,
            status=status.HTTP_200_OK,
        )

    def _cleanup_reviewer_assignments(self, coi: models.ConflictOfInterest):
        """
        Clean up any existing assignments and reviews when a reviewer is recused.

        This ensures:
        1. Any pending assignment items are marked as COI_BLOCKED
        2. Any active reviews are rejected
        3. The COI record is linked to the blocked assignments
        """
        reviewer = coi.reviewer
        proposal = coi.proposal

        if not reviewer or not proposal:
            return

        # Find assignment items for this reviewer-proposal pair
        assignment_items = models.AssignmentItem.objects.filter(
            proposal=proposal,
            batch__reviewer_pool_entry__reviewer=reviewer,
            status__in=[
                models.AssignmentItemStatuses.PENDING,
                models.AssignmentItemStatuses.ACCEPTED,
            ],
        )

        for item in assignment_items:
            # Mark the item as COI blocked
            item.status = models.AssignmentItemStatuses.COI_BLOCKED
            item.has_coi = True
            item.save(update_fields=["status", "has_coi"])
            item.coi_records.add(coi)

            # If the item had an associated review, reject it
            if item.review:
                item.review.state = models.Review.States.REJECTED
                item.review.save(update_fields=["state"])

        # Also reject any reviews directly (in case they exist without assignment items)
        models.Review.objects.filter(
            proposal=proposal,
            reviewer=reviewer.user,
            state__in=[models.Review.States.IN_REVIEW],
        ).update(state=models.Review.States.REJECTED)

    def _unblock_related_assignments(self, coi):
        """
        Unblock assignment items that were blocked by this COI.

        Only unblocks items if no other unresolved COIs remain for
        the same reviewer-proposal pair.
        """
        if not coi.proposal or not coi.reviewer:
            return

        blocked_items = models.AssignmentItem.objects.filter(
            proposal=coi.proposal,
            batch__reviewer_pool_entry__reviewer=coi.reviewer,
            status=models.AssignmentItemStatuses.COI_BLOCKED,
        )

        for item in blocked_items:
            # Check if there are other unresolved COIs (pending or recused)
            # excluding the one being dismissed/waived
            other_blocking_cois = item.coi_records.filter(
                status__in=[COIStatuses.PENDING, COIStatuses.RECUSED],
            ).exclude(pk=coi.pk)

            if not other_blocking_cois.exists():
                item.status = models.AssignmentItemStatuses.PENDING
                item.has_coi = False
                item.save(update_fields=["status", "has_coi"])
                item.coi_records.remove(coi)

    dismiss_permissions = waive_permissions = recuse_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]
    dismiss_serializer_class = waive_serializer_class = recuse_serializer_class = (
        serializers.COIStatusUpdateSerializer
    )


class COIDisclosureViewSet(ActionsViewSet):
    """ViewSet for COI disclosure forms."""

    lookup_field = "uuid"
    queryset = models.COIDisclosureForm.objects.all().order_by("-created")
    serializer_class = serializers.COIDisclosureFormSerializer
    filterset_class = filters.COIDisclosureFormFilter
    filter_backends = (DjangoFilterBackend,)
    disabled_actions = ["update", "partial_update", "destroy"]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.COIDisclosureForm.objects.all().order_by("-created")
        # Users can see their own disclosures, call managers can see all for their calls
        return models.COIDisclosureForm.objects.filter(
            Q(reviewer__user=user)
            | Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-created")

    def perform_create(self, serializer):
        # Get or create reviewer profile
        try:
            reviewer_profile = models.ReviewerProfile.objects.get(
                user=self.request.user
            )
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must create a reviewer profile before submitting a disclosure.")
            )
        serializer.save(
            reviewer=reviewer_profile,
            certified=True,
            certification_date=timezone.now(),
        )


# =============================================================================
# Invitation Acceptance Mixin (shared logic for pool invitation handling)
# =============================================================================


class InvitationAcceptanceMixin:
    """
    Mixin providing common logic for accepting/declining reviewer pool invitations.

    Used by both CallReviewerPoolViewSet and PublicReviewerInvitationViewSet
    to avoid code duplication.
    """

    def _validate_invitation_status(self, invitation: models.CallReviewerPool):
        """Validate that the invitation is still pending."""
        if invitation.invitation_status != ReviewerPoolInvitationStatuses.PENDING:
            raise exceptions.ValidationError(
                _("This invitation has already been responded to.")
            )

    def _validate_invitation_not_expired(self, invitation: models.CallReviewerPool):
        """Validate that the invitation has not expired."""
        if (
            invitation.invitation_expires_at
            and invitation.invitation_expires_at < timezone.now()
        ):
            raise exceptions.ValidationError(_("This invitation has expired."))

    def _ensure_published_profile(
        self, request, invitation: models.CallReviewerPool
    ) -> tuple[models.ReviewerProfile | None, dict | None]:
        """
        Ensure the user has a published reviewer profile for email-based invitations.

        Returns:
            tuple: (profile, error_response) - profile if found, or error_response dict if not
        """
        if invitation.reviewer:
            return invitation.reviewer, None

        # Check if user is authenticated
        if not request.user.is_authenticated:
            return None, {
                "error": _("Please log in to accept this invitation."),
                "status": status.HTTP_401_UNAUTHORIZED,
            }

        # Check if user has a published profile
        try:
            profile = models.ReviewerProfile.objects.get(
                user=request.user,
                is_published=True,
            )
            return profile, None
        except models.ReviewerProfile.DoesNotExist:
            has_unpublished = models.ReviewerProfile.objects.filter(
                user=request.user, is_published=False
            ).exists()

            if has_unpublished:
                return None, {
                    "error": "profile_not_published",
                    "message": _(
                        "Please publish your reviewer profile before accepting."
                    ),
                    "profile_url": "/api/reviewer-profiles/me/",
                    "status": status.HTTP_400_BAD_REQUEST,
                }
            else:
                return None, {
                    "error": "profile_required",
                    "message": _(
                        "Please create and publish your reviewer profile first."
                    ),
                    "profile_url": "/api/reviewer-profiles/me/",
                    "status": status.HTTP_400_BAD_REQUEST,
                }

    def _process_self_declared_conflicts(
        self,
        declared_conflicts: list,
        invitation: models.CallReviewerPool,
    ) -> list[str]:
        """
        Process self-declared conflicts from invitation acceptance.

        Args:
            declared_conflicts: List of conflict data from request
            invitation: The invitation being accepted

        Returns:
            List of created conflict UUIDs
        """
        if not declared_conflicts:
            return []

        conflict_serializer = serializers.SelfDeclaredConflictSerializer(
            data=declared_conflicts,
            many=True,
            context={"call": invitation.call, "reviewer": invitation.reviewer},
        )
        conflict_serializer.is_valid(raise_exception=True)

        created_conflicts = []
        for conflict_data in conflict_serializer.validated_data:
            coi = models.ConflictOfInterest.objects.create(
                reviewer=invitation.reviewer,
                call=invitation.call,
                proposal=conflict_data["proposal_uuid"],
                coi_type=conflict_data["coi_type"],
                severity=conflict_data.get("severity", COISeverityLevels.APPARENT),
                detection_method=COIDetectionMethods.SELF_DISCLOSED,
                evidence_description=conflict_data.get("description", ""),
                status=COIStatuses.PENDING,
            )
            created_conflicts.append(str(coi.uuid))

        return created_conflicts

    def _accept_invitation(
        self, invitation: models.CallReviewerPool
    ) -> models.CallReviewerPool:
        """Mark the invitation as accepted."""
        invitation.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
        invitation.response_date = timezone.now()
        invitation.save()
        return invitation

    def _decline_invitation(
        self, invitation: models.CallReviewerPool, reason: str = ""
    ) -> models.CallReviewerPool:
        """Mark the invitation as declined."""
        invitation.invitation_status = ReviewerPoolInvitationStatuses.DECLINED
        invitation.response_date = timezone.now()
        invitation.decline_reason = reason
        invitation.save()
        return invitation


class CallReviewerPoolViewSet(InvitationAcceptanceMixin, ActionsViewSet):
    """ViewSet for call reviewer pool management."""

    lookup_field = "uuid"
    queryset = models.CallReviewerPool.objects.all().order_by("call", "reviewer")
    serializer_class = serializers.CallReviewerPoolSerializer
    filterset_class = filters.CallReviewerPoolFilter
    filter_backends = (DjangoFilterBackend,)

    # Only allow partial_update (PATCH), disable create/update/destroy
    disabled_actions = ["create", "update", "destroy"]

    partial_update_serializer_class = serializers.CallReviewerPoolUpdateSerializer
    partial_update_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            qs = models.CallReviewerPool.objects.all()
        else:
            qs = models.CallReviewerPool.objects.filter(
                Q(call__in=get_connected_calls(user, CallRole.MANAGER))
                | Q(call__manager__customer__in=get_connected_customers(user))
                | Q(
                    call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                        user
                    )
                )
                | Q(reviewer__user=user)
                | Q(invited_user=user)  # Include user-based invitations
                | Q(invited_email=user.email)  # Include email-based invitations
            )
        # Add select_related to avoid N+1 on related fields
        return qs.select_related(
            "call",
            "reviewer",
            "reviewer__user",
            "invited_by",
            "invited_user",
        ).order_by("call", "reviewer")

    def get_serializer_context(self):
        """Add prefetched COI and review counts to context to avoid N+1 queries."""
        context = super().get_serializer_context()

        # Only prefetch for list actions
        if self.action not in ["list", "retrieve"]:
            return context

        # Get the queryset that will be serialized
        queryset = self.filter_queryset(self.get_queryset())
        pool_members = list(queryset)

        if not pool_members:
            return context

        # Collect all call_ids and reviewer_ids
        call_ids = {pm.call_id for pm in pool_members}
        reviewer_ids = {pm.reviewer_id for pm in pool_members if pm.reviewer_id}
        user_ids = set()
        for pm in pool_members:
            if pm.reviewer and pm.reviewer.user_id:
                user_ids.add(pm.reviewer.user_id)
            elif pm.invited_user_id:
                user_ids.add(pm.invited_user_id)

        # Prefetch COI counts: (reviewer_id, call_id) -> count
        coi_counts = {}
        coi_by_severity = {}
        if reviewer_ids and call_ids:
            coi_data = (
                models.ConflictOfInterest.objects.filter(
                    reviewer_id__in=reviewer_ids,
                    call_id__in=call_ids,
                )
                .values("reviewer_id", "call_id", "severity")
                .annotate(count=Count("id"))
            )
            for item in coi_data:
                key = (item["reviewer_id"], item["call_id"])
                coi_counts[key] = coi_counts.get(key, 0) + item["count"]
                if key not in coi_by_severity:
                    coi_by_severity[key] = {}
                coi_by_severity[key][item["severity"]] = item["count"]

        # Prefetch review counts: (user_id, call_id) -> {state: count}
        review_counts = {}
        if user_ids and call_ids:
            review_data = (
                models.Review.objects.filter(
                    reviewer_id__in=user_ids,
                    proposal__round__call_id__in=call_ids,
                )
                .values("reviewer_id", "proposal__round__call_id", "state")
                .annotate(count=Count("id"))
            )
            for item in review_data:
                key = (item["reviewer_id"], item["proposal__round__call_id"])
                if key not in review_counts:
                    review_counts[key] = {}
                review_counts[key][item["state"]] = item["count"]

        context["coi_counts"] = coi_counts
        context["coi_by_severity"] = coi_by_severity
        context["review_counts"] = review_counts

        return context

    def _verify_invitation_ownership(self, invitation):
        """Verify the current user owns this invitation."""
        user = self.request.user
        if invitation.reviewer and invitation.reviewer.user == user:
            return True
        if invitation.invited_user == user:
            return True
        if invitation.invited_email and invitation.invited_email == user.email:
            return True
        return False

    @extend_schema(
        description="Accept a pool invitation (authenticated users only).",
        request=serializers.SelfDeclaredConflictSerializer(many=True),
        responses={
            200: serializers.InvitationAcceptResponseSerializer,
            400: serializers.InvitationAcceptErrorSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        """Accept a pool invitation."""
        invitation = self.get_object()

        # Verify ownership
        if not self._verify_invitation_ownership(invitation):
            raise exceptions.PermissionDenied(
                _("You do not have permission to accept this invitation.")
            )

        # Use mixin methods for validation
        self._validate_invitation_status(invitation)
        self._validate_invitation_not_expired(invitation)

        # Profile-gating: user must have a published reviewer profile
        profile, error = self._ensure_published_profile(request, invitation)
        if error:
            error_status = error.pop("status", status.HTTP_400_BAD_REQUEST)
            return response.Response(error, status=error_status)

        # Link profile to invitation if needed
        if not invitation.reviewer:
            invitation.reviewer = profile
            if not invitation.invited_user:
                invitation.invited_user = request.user

        # Process optional self-declared conflicts
        # Body is the array of conflicts directly (not wrapped in a dict)
        declared_conflicts = request.data if isinstance(request.data, list) else []
        created_conflicts = self._process_self_declared_conflicts(
            declared_conflicts, invitation
        )

        self._accept_invitation(invitation)

        result = {"detail": _("Invitation accepted successfully.")}
        if created_conflicts:
            result["declared_conflicts"] = created_conflicts
        return response.Response(result)

    @extend_schema(
        description="Decline a pool invitation (authenticated users only).",
        request=serializers.InvitationDeclineSerializer,
        responses={200: serializers.InvitationDeclineResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        """Decline a pool invitation."""
        invitation = self.get_object()

        # Verify ownership
        if not self._verify_invitation_ownership(invitation):
            raise exceptions.PermissionDenied(
                _("You do not have permission to decline this invitation.")
            )

        self._validate_invitation_status(invitation)

        serializer = serializers.InvitationDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        self._decline_invitation(
            invitation, serializer.validated_data.get("reason", "")
        )

        return response.Response({"detail": _("Invitation declined.")})

    @extend_schema(
        description="Force-accept a pool invitation (manager override).",
        request=serializers.ForceAcceptPoolSerializer,
        responses={200: serializers.CallReviewerPoolSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="force-accept")
    def force_accept(self, request, uuid=None):
        """Force-accept a pool invitation with a reason."""
        invitation = self.get_object()

        if invitation.invitation_status == ReviewerPoolInvitationStatuses.ACCEPTED:
            return response.Response(
                {"error": _("This invitation is already accepted.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if invitation.invitation_status not in [
            ReviewerPoolInvitationStatuses.PENDING,
            ReviewerPoolInvitationStatuses.DECLINED,
            ReviewerPoolInvitationStatuses.EXPIRED,
        ]:
            return response.Response(
                {"error": _("This invitation cannot be force-accepted.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not invitation.reviewer:
            return response.Response(
                {
                    "error": _(
                        "Cannot force-accept an email-only invitation without a reviewer profile."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ForceAcceptPoolSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invitation.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
        invitation.response_date = timezone.now()
        invitation.override_reason = serializer.validated_data["override_reason"]
        invitation.overridden_by = request.user
        invitation.overridden_at = timezone.now()
        invitation.save()

        return response.Response(
            serializers.CallReviewerPoolSerializer(
                invitation, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    force_accept_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]


class COIDetectionJobViewSet(ReadOnlyActionsViewSet):
    """ViewSet for viewing COI detection job status."""

    lookup_field = "uuid"
    queryset = models.COIDetectionJob.objects.all().order_by("-created")
    serializer_class = serializers.COIDetectionJobSerializer
    filterset_class = filters.COIDetectionJobFilter
    filter_backends = (DjangoFilterBackend,)

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.COIDetectionJob.objects.all().order_by("-created")
        return models.COIDetectionJob.objects.filter(
            Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-created")


# =============================================================================
# Reviewer Suggestion ViewSet
# =============================================================================


class ReviewerSuggestionViewSet(ReadOnlyActionsViewSet):
    """ViewSet for managing algorithm-generated reviewer suggestions."""

    lookup_field = "uuid"
    queryset = models.ReviewerSuggestion.objects.all().order_by("-affinity_score")
    serializer_class = serializers.ReviewerSuggestionSerializer
    filterset_class = filters.ReviewerSuggestionFilter
    filter_backends = (DjangoFilterBackend,)
    # Allow destroy action (override ReadOnlyActionsViewSet defaults)
    disabled_actions = ["create", "update", "partial_update"]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.ReviewerSuggestion.objects.all().order_by("-affinity_score")
        return models.ReviewerSuggestion.objects.filter(
            Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-affinity_score")

    @extend_schema(
        description="Delete a reviewer suggestion.",
        responses={204: None},
    )
    def destroy(self, request, uuid=None):
        """Delete a reviewer suggestion."""
        suggestion = self.get_object()

        # Check if user has permission to manage this call
        user = request.user
        call = suggestion.call
        if not user.is_staff and not (
            call.id in get_connected_calls(user, CallRole.MANAGER)
            or call.manager.customer_id in get_connected_customers(user)
        ):
            raise exceptions.PermissionDenied(
                _("You do not have permission to delete suggestions for this call.")
            )

        suggestion.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]

    @extend_schema(
        description="Confirm a reviewer suggestion. The reviewer will be invited to the call.",
        responses={200: serializers.ReviewerSuggestionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def confirm(self, request, uuid=None):
        """Manager confirms a reviewer suggestion."""
        suggestion = self.get_object()

        # Check if user has permission to manage this call
        user = request.user
        call = suggestion.call
        if not user.is_staff and not (
            call.id in get_connected_calls(user, CallRole.MANAGER)
            or call.manager.customer_id in get_connected_customers(user)
        ):
            raise exceptions.PermissionDenied(
                _("You do not have permission to confirm suggestions for this call.")
            )

        if suggestion.status != ReviewerSuggestionStatuses.PENDING:
            raise exceptions.ValidationError(
                _("Only pending suggestions can be confirmed.")
            )

        suggestion.status = ReviewerSuggestionStatuses.CONFIRMED
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.save()

        return response.Response(
            serializers.ReviewerSuggestionSerializer(
                suggestion, context={"request": request}
            ).data
        )

    @extend_schema(
        description="Reject a reviewer suggestion.",
        request=serializers.SuggestionRejectSerializer,
        responses={200: serializers.ReviewerSuggestionSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        """Manager rejects a reviewer suggestion."""
        suggestion = self.get_object()

        # Check if user has permission to manage this call
        user = request.user
        call = suggestion.call
        if not user.is_staff and not (
            call.id in get_connected_calls(user, CallRole.MANAGER)
            or call.manager.customer_id in get_connected_customers(user)
        ):
            raise exceptions.PermissionDenied(
                _("You do not have permission to reject suggestions for this call.")
            )

        if suggestion.status != ReviewerSuggestionStatuses.PENDING:
            raise exceptions.ValidationError(
                _("Only pending suggestions can be rejected.")
            )

        reason = request.data.get("reason", "")
        suggestion.status = ReviewerSuggestionStatuses.REJECTED
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.rejection_reason = reason
        suggestion.save()

        return response.Response(
            serializers.ReviewerSuggestionSerializer(
                suggestion, context={"request": request}
            ).data
        )


# =============================================================================
# Public Reviewer Invitation ViewSet
# =============================================================================


class PublicReviewerInvitationViewSet(InvitationAcceptanceMixin, viewsets.ViewSet):
    """
    Public endpoints for handling reviewer invitations via token.

    These endpoints do not require authentication - the invitation token
    serves as authorization.
    """

    permission_classes = []  # Public - no auth required

    def _get_invitation(self, token: str) -> models.CallReviewerPool:
        """Get and validate invitation by token."""
        try:
            invitation = models.CallReviewerPool.objects.select_related(
                "call", "reviewer__user"
            ).get(invitation_token=token)
        except models.CallReviewerPool.DoesNotExist:
            raise exceptions.NotFound(_("Invalid invitation token."))

        return invitation

    @extend_schema(
        description="Get invitation details by token.",
        responses=serializers.PublicInvitationSerializer,
    )
    def retrieve(self, request, token=None):
        """Get invitation details by token including COI configuration."""
        invitation = self._get_invitation(token)
        call = invitation.call

        is_expired = (
            invitation.invitation_expires_at
            and invitation.invitation_expires_at < timezone.now()
        )

        # Check user's profile status if authenticated
        profile_status = None
        if request.user.is_authenticated:
            profile = models.ReviewerProfile.objects.filter(user=request.user).first()
            if profile:
                profile_status = "published" if profile.is_published else "unpublished"
            else:
                profile_status = "missing"

        # Get COI configuration if available (informational only at invitation stage)
        coi_config = None
        if hasattr(call, "coi_configuration") and call.coi_configuration:
            config = call.coi_configuration
            coi_config = {
                "recusal_required_types": config.recusal_required_types,
                "management_allowed_types": config.management_allowed_types,
                "disclosure_only_types": config.disclosure_only_types,
            }

        # Note: Proposals are NOT included at invitation stage.
        # They are only disclosed during the assignment stage (two-step workflow).
        return response.Response(
            {
                "call_name": call.name,
                "call_uuid": str(call.uuid),
                "invitation_status": invitation.invitation_status,
                "expires_at": invitation.invitation_expires_at,
                "is_expired": is_expired,
                "max_assignments": invitation.max_assignments,
                "invited_by_name": (
                    invitation.invited_by.full_name if invitation.invited_by else None
                ),
                "profile_status": profile_status,
                "requires_profile": invitation.reviewer is None,
                "coi_configuration": coi_config,
                "coi_types": COITypes.CHOICES,
            }
        )

    @extend_schema(
        description="Accept a reviewer invitation.",
        request=serializers.InvitationAcceptSerializer,
        responses={
            200: serializers.InvitationAcceptResponseSerializer,
            400: serializers.InvitationAcceptErrorSerializer,
            401: serializers.InvitationAuthErrorSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, token=None):
        """Accept a reviewer invitation."""
        invitation = self._get_invitation(token)

        # Use mixin methods for validation
        self._validate_invitation_status(invitation)
        self._validate_invitation_not_expired(invitation)

        # Profile-gating for email invitations
        profile, error = self._ensure_published_profile(request, invitation)
        if error:
            error_status = error.pop("status", status.HTTP_400_BAD_REQUEST)
            return response.Response(error, status=error_status)

        # Link profile to invitation if needed
        if not invitation.reviewer:
            invitation.reviewer = profile
            if not invitation.invited_user:
                invitation.invited_user = request.user

        # Process optional self-declared conflicts
        declared_conflicts = request.data.get("declared_conflicts", [])
        created_conflicts = self._process_self_declared_conflicts(
            declared_conflicts, invitation
        )

        self._accept_invitation(invitation)

        result = {"detail": _("Invitation accepted successfully.")}
        if created_conflicts:
            result["declared_conflicts"] = created_conflicts
        return response.Response(result)

    @extend_schema(
        description="Decline a reviewer invitation.",
        request=serializers.InvitationDeclineSerializer,
        responses={200: serializers.InvitationDeclineResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def decline(self, request, token=None):
        """Decline a reviewer invitation."""
        invitation = self._get_invitation(token)

        self._validate_invitation_status(invitation)

        serializer = serializers.InvitationDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        self._decline_invitation(
            invitation, serializer.validated_data.get("reason", "")
        )

        return response.Response({"detail": _("Invitation declined.")})


# =============================================================================
# Reviewer Bids ViewSet
# =============================================================================


class ReviewerBidViewSet(ActionsViewSet):
    """
    ViewSet for managing reviewer bids on proposals.

    Reviewers can indicate their preference/availability for reviewing proposals.
    """

    lookup_field = "uuid"
    queryset = models.ReviewerBid.objects.all().order_by("-submitted_at")
    serializer_class = serializers.ReviewerBidSerializer
    filterset_class = filters.ReviewerBidFilter
    filter_backends = (DjangoFilterBackend,)

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return models.ReviewerBid.objects.all().order_by("-submitted_at")
        # Reviewers can see their own bids, managers can see all for their calls
        return models.ReviewerBid.objects.filter(
            Q(reviewer__user=user)
            | Q(call__in=get_connected_calls(user, CallRole.MANAGER))
            | Q(call__manager__customer__in=get_connected_customers(user))
            | Q(
                call__manager__customer__callmanagingorganisation__in=get_connected_call_organizers(
                    user
                )
            )
        ).order_by("-submitted_at")

    @extend_schema(
        description="Get my bids for a specific call.",
        responses=serializers.ReviewerBidSerializer(many=True),
    )
    @decorators.action(detail=False, methods=["get"], url_path="my-bids")
    def my_bids(self, request):
        """Get current user's bids, optionally filtered by call."""
        call_uuid = request.query_params.get("call_uuid")

        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            return response.Response([])

        bids = models.ReviewerBid.objects.filter(reviewer=profile)
        if call_uuid:
            bids = bids.filter(call__uuid=call_uuid)

        serializer = self.get_serializer(bids, many=True)
        return response.Response(serializer.data)

    @extend_schema(
        description="Submit a bid on a proposal.",
        request=serializers.ReviewerBidSubmitSerializer,
        responses=serializers.ReviewerBidSerializer,
    )
    @decorators.action(detail=False, methods=["post"], url_path="submit")
    def submit_bid(self, request):
        """Submit a bid on a proposal."""
        serializer = serializers.ReviewerBidSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must have a reviewer profile to submit bids.")
            )

        try:
            proposal = models.Proposal.objects.get(
                uuid=serializer.validated_data["proposal_uuid"]
            )
        except models.Proposal.DoesNotExist:
            raise exceptions.ValidationError(
                {"proposal_uuid": _("Proposal not found.")}
            )

        # Get the call from the proposal
        call = proposal.round.call if hasattr(proposal, "round") else None
        if not call:
            raise exceptions.ValidationError(
                _("Could not determine the call for this proposal.")
            )

        # Check if reviewer is in the pool for this call
        if not models.CallReviewerPool.objects.filter(
            call=call,
            reviewer=profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        ).exists():
            raise exceptions.ValidationError(
                _("You are not in the reviewer pool for this call.")
            )

        # Create or update bid
        bid, created = models.ReviewerBid.objects.update_or_create(
            call=call,
            reviewer=profile,
            proposal=proposal,
            defaults={
                "bid": serializer.validated_data["bid"],
                "comment": serializer.validated_data.get("comment", ""),
            },
        )

        return response.Response(
            self.get_serializer(bid).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        description="Submit multiple bids at once.",
        request=serializers.ReviewerBulkBidSerializer,
        responses={
            200: {"type": "object", "properties": {"submitted": {"type": "integer"}}}
        },
    )
    @decorators.action(detail=False, methods=["post"], url_path="bulk-submit")
    def bulk_submit(self, request):
        """Submit multiple bids at once."""
        serializer = serializers.ReviewerBulkBidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            profile = models.ReviewerProfile.objects.get(user=request.user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.ValidationError(
                _("You must have a reviewer profile to submit bids.")
            )

        submitted = 0
        errors = []

        for bid_data in serializer.validated_data["bids"]:
            try:
                proposal = models.Proposal.objects.get(uuid=bid_data["proposal_uuid"])
                call = proposal.round.call if hasattr(proposal, "round") else None

                if not call:
                    errors.append(f"No call for proposal {bid_data['proposal_uuid']}")
                    continue

                models.ReviewerBid.objects.update_or_create(
                    call=call,
                    reviewer=profile,
                    proposal=proposal,
                    defaults={
                        "bid": bid_data["bid"],
                        "comment": bid_data.get("comment", ""),
                    },
                )
                submitted += 1
            except models.Proposal.DoesNotExist:
                errors.append(f"Proposal {bid_data['proposal_uuid']} not found")

        return response.Response(
            {
                "submitted": submitted,
                "errors": errors if errors else None,
            }
        )


# Nested ViewSets for ReviewerProfile sub-resources


@extend_schema_view(
    list=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_list",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    create=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_create",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    retrieve=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_retrieve",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    update=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    partial_update=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_partial_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    destroy=extend_schema(
        operation_id="nested_reviewer_profile_affiliations_destroy",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
)
class ReviewerProfileAffiliationViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for managing reviewer profile affiliations."""

    lookup_field = "uuid"
    queryset = models.ReviewerAffiliation.objects.all()
    serializer_class = serializers.ReviewerAffiliationSerializer
    filter_backends = (DjangoFilterBackend,)

    def get_reviewer_profile(self):
        profile_uuid = self.kwargs.get("reviewer_profile_uuid")
        return get_object_or_404(models.ReviewerProfile, uuid=profile_uuid)

    def check_permission(self):
        profile = self.get_reviewer_profile()
        user = self.request.user
        # Only profile owner or staff can manage affiliations
        if not (user.is_staff or profile.user == user):
            raise exceptions.PermissionDenied(
                _("You do not have permission to manage this reviewer profile.")
            )

    def get_queryset(self):
        self.check_permission()
        profile = self.get_reviewer_profile()
        return profile.affiliations.all()

    def perform_create(self, serializer):
        self.check_permission()
        profile = self.get_reviewer_profile()
        serializer.save(reviewer_profile=profile)


@extend_schema_view(
    list=extend_schema(
        operation_id="nested_reviewer_profile_expertise_list",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    create=extend_schema(
        operation_id="nested_reviewer_profile_expertise_create",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    retrieve=extend_schema(
        operation_id="nested_reviewer_profile_expertise_retrieve",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    update=extend_schema(
        operation_id="nested_reviewer_profile_expertise_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    partial_update=extend_schema(
        operation_id="nested_reviewer_profile_expertise_partial_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    destroy=extend_schema(
        operation_id="nested_reviewer_profile_expertise_destroy",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
)
class ReviewerProfileExpertiseViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for managing reviewer profile expertise."""

    lookup_field = "uuid"
    queryset = models.ReviewerExpertise.objects.all()
    serializer_class = serializers.ReviewerExpertiseSerializer
    filter_backends = (DjangoFilterBackend,)

    def get_reviewer_profile(self):
        profile_uuid = self.kwargs.get("reviewer_profile_uuid")
        return get_object_or_404(models.ReviewerProfile, uuid=profile_uuid)

    def check_permission(self):
        profile = self.get_reviewer_profile()
        user = self.request.user
        # Only profile owner or staff can manage expertise
        if not (user.is_staff or profile.user == user):
            raise exceptions.PermissionDenied(
                _("You do not have permission to manage this reviewer profile.")
            )

    def get_queryset(self):
        self.check_permission()
        profile = self.get_reviewer_profile()
        return profile.expertise_set.all()

    def perform_create(self, serializer):
        self.check_permission()
        profile = self.get_reviewer_profile()
        serializer.save(reviewer_profile=profile)


@extend_schema_view(
    list=extend_schema(
        operation_id="nested_reviewer_profile_publications_list",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    create=extend_schema(
        operation_id="nested_reviewer_profile_publications_create",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    retrieve=extend_schema(
        operation_id="nested_reviewer_profile_publications_retrieve",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    update=extend_schema(
        operation_id="nested_reviewer_profile_publications_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    partial_update=extend_schema(
        operation_id="nested_reviewer_profile_publications_partial_update",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
    destroy=extend_schema(
        operation_id="nested_reviewer_profile_publications_destroy",
        parameters=[
            OpenApiParameter(
                name="reviewer_profile_uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the parent reviewer profile",
            )
        ],
    ),
)
class ReviewerProfilePublicationViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for managing reviewer profile publications."""

    lookup_field = "uuid"
    queryset = models.ReviewerPublication.objects.all()
    serializer_class = serializers.ReviewerPublicationSerializer
    filter_backends = (DjangoFilterBackend,)

    def get_reviewer_profile(self):
        profile_uuid = self.kwargs.get("reviewer_profile_uuid")
        return get_object_or_404(models.ReviewerProfile, uuid=profile_uuid)

    def check_permission(self):
        profile = self.get_reviewer_profile()
        user = self.request.user
        # Only profile owner or staff can manage publications
        if not (user.is_staff or profile.user == user):
            raise exceptions.PermissionDenied(
                _("You do not have permission to manage this reviewer profile.")
            )

    def get_queryset(self):
        self.check_permission()
        profile = self.get_reviewer_profile()
        return profile.publications.all()

    def perform_create(self, serializer):
        self.check_permission()
        profile = self.get_reviewer_profile()
        serializer.save(reviewer_profile=profile)


# =============================================================================
# Assignment Batch ViewSets (Stage 2 - Proposal Assignment Workflow)
# =============================================================================


class AssignmentBatchViewSet(ActionsViewSet):
    """
    ViewSet for managing assignment batches.

    Assignment batches are created when a call manager generates assignments
    for reviewers. Each batch contains one or more proposal assignments for
    a single reviewer.
    """

    lookup_field = "uuid"
    queryset = models.AssignmentBatch.objects.all().order_by("-created")
    serializer_class = serializers.AssignmentBatchSerializer
    filterset_class = filters.AssignmentBatchFilter

    # Permissions for custom actions - managers only
    send_permissions = cancel_permissions = extend_deadline_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["call", "call.manager"],
        )
    ]

    def get_serializer_class(self):
        if self.action == "list":
            return serializers.AssignmentBatchListSerializer
        return serializers.AssignmentBatchSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff:
            return queryset

        # Filter based on user's roles
        return queryset.filter(models.filter_assignment_batches(user))

    @extend_schema(
        description="Send this assignment batch invitation to the reviewer.",
        request=serializers.SendAssignmentBatchSerializer,
        responses={200: serializers.SendAssignmentBatchResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def send(self, request, uuid=None):
        """Send the assignment batch invitation to the reviewer."""
        batch: models.AssignmentBatch = self.get_object()

        if batch.status != models.AssignmentBatchStatuses.DRAFT:
            return response.Response(
                {"error": _("Only draft batches can be sent.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.SendAssignmentBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("manager_notes"):
            batch.manager_notes = serializer.validated_data["manager_notes"]

        batch.send_invitation(user=request.user)

        return response.Response(
            {
                "detail": _("Assignment batch invitation sent successfully."),
                "expires_at": batch.expires_at,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Cancel this assignment batch.",
        request=None,
        responses={200: serializers.MessageResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        """Cancel the assignment batch."""
        batch: models.AssignmentBatch = self.get_object()

        if batch.status not in [
            models.AssignmentBatchStatuses.DRAFT,
            models.AssignmentBatchStatuses.SENT,
        ]:
            return response.Response(
                {"error": _("Only draft or sent batches can be cancelled.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.status = models.AssignmentBatchStatuses.CANCELLED
        batch.save(update_fields=["status"])

        return response.Response(
            {"message": _("Assignment batch cancelled.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Extend or modify the expiration date for an assignment batch. "
        "Can reactivate expired batches by setting a future deadline.",
        request=serializers.ExtendDeadlineRequestSerializer,
        responses={200: serializers.ExtendDeadlineResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="extend-deadline")
    def extend_deadline(self, request, uuid=None):
        """
        Extend or modify the expiration date for an assignment batch.

        This allows call managers to:
        - Extend the deadline for sent batches before they expire
        - Reactivate expired batches by setting a new future deadline

        If a batch is in EXPIRED status and a future deadline is set,
        the batch will be reactivated to SENT status.
        """
        batch: models.AssignmentBatch = self.get_object()

        if batch.status not in [
            models.AssignmentBatchStatuses.SENT,
            models.AssignmentBatchStatuses.EXPIRED,
        ]:
            return response.Response(
                {"error": _("Can only extend deadline for sent or expired batches.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ExtendDeadlineRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_expires_at = serializer.validated_data["expires_at"]
        batch.expires_at = new_expires_at

        # Reactivate expired batch if new deadline is in future
        if batch.status == models.AssignmentBatchStatuses.EXPIRED:
            batch.status = models.AssignmentBatchStatuses.SENT
            batch.manager_notified = False  # Reset notification flag
            batch.reminder_sent = False  # Reset reminder flag
            # Also reactivate pending items
            batch.items.filter(status=models.AssignmentItemStatuses.EXPIRED).update(
                status=models.AssignmentItemStatuses.PENDING
            )

        batch.save(
            update_fields=["expires_at", "status", "manager_notified", "reminder_sent"]
        )

        # Sync review deadlines for accepted assignments
        # This ensures reviews don't get auto-rejected by the expiry task
        # before the extended deadline
        accepted_items_with_reviews = batch.items.filter(
            status=models.AssignmentItemStatuses.ACCEPTED,
            review__isnull=False,
        ).select_related("review")

        for item in accepted_items_with_reviews:
            if (
                item.review.review_end_date
                and item.review.review_end_date < new_expires_at
            ):
                item.review.review_end_date = new_expires_at
                item.review.save(update_fields=["review_end_date"])

        return response.Response(
            {
                "expires_at": batch.expires_at,
                "status": batch.status,
            },
            status=status.HTTP_200_OK,
        )


class AssignmentItemViewSet(ActionsViewSet):
    """
    ViewSet for managing individual assignment items.

    Each item represents a proposal assignment within a batch.
    Reviewers can accept or decline items individually.
    """

    lookup_field = "uuid"
    queryset = models.AssignmentItem.objects.all().order_by("-created")
    serializer_class = serializers.AssignmentItemSerializer
    filterset_class = filters.AssignmentItemFilter

    # Permissions for manager-only actions
    suggest_alternatives_permissions = reassign_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["batch.call", "batch.call.manager"],
        )
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff:
            return queryset

        # Filter based on user's roles
        return queryset.filter(models.filter_assignment_items(user))

    @extend_schema(
        description="Accept this assignment item. Creates a Review record.",
        request=serializers.AssignmentItemAcceptSerializer,
        responses={200: serializers.AssignmentItemResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        """Accept the assignment and create a Review record."""
        item: models.AssignmentItem = self.get_object()

        # Block responses to expired batches
        if item.batch.is_expired:
            return response.Response(
                {"error": _("Cannot accept expired assignment.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            review = item.accept(user=request.user)
            return response.Response(
                {
                    "detail": _("Assignment accepted. Review created."),
                    "review_uuid": str(review.uuid),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        description="Decline this assignment item.",
        request=serializers.AssignmentItemDeclineSerializer,
        responses={200: serializers.AssignmentItemResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        """Decline the assignment with an optional reason."""
        item: models.AssignmentItem = self.get_object()

        # Block responses to expired batches
        if item.batch.is_expired:
            return response.Response(
                {"error": _("Cannot decline expired assignment.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.AssignmentItemDeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            item.decline(
                reason=serializer.validated_data.get("reason", ""),
                user=request.user,
            )
            return response.Response(
                {
                    "detail": _("Assignment declined."),
                    "review_uuid": None,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return response.Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        description="Suggest alternative reviewers for a declined assignment.",
        request=None,
        responses={200: serializers.SuggestAlternativeReviewersSerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def suggest_alternatives(self, request, uuid=None):
        """Get alternative reviewer suggestions for a declined item."""
        item: models.AssignmentItem = self.get_object()

        if item.status != models.AssignmentItemStatuses.DECLINED:
            return response.Response(
                {"error": _("Alternatives can only be suggested for declined items.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call = item.batch.call
        proposal = item.proposal

        # Get pool members who:
        # 1. Have accepted invitation
        # 2. Don't already have this proposal assigned
        # 3. Don't have blocking COI
        # 4. Haven't declined this proposal before
        existing_reviewers = models.AssignmentItem.objects.filter(
            proposal=proposal,
        ).values_list("batch__reviewer_pool_entry_id", flat=True)

        declined_reviewers = models.AssignmentItem.objects.filter(
            proposal=proposal,
            status=models.AssignmentItemStatuses.DECLINED,
        ).values_list("batch__reviewer_pool_entry_id", flat=True)

        pool_entries = (
            models.CallReviewerPool.objects.filter(
                call=call,
                invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            )
            .exclude(id__in=existing_reviewers)
            .exclude(id__in=declined_reviewers)
        )

        # Get affinity scores for suggestions
        suggestions = []
        for entry in pool_entries[:10]:  # Limit to 10 suggestions
            affinity = models.ReviewerProposalAffinity.objects.filter(
                reviewer=entry.reviewer,
                proposal=proposal,
            ).first()

            # Check COI
            has_coi = models.ConflictOfInterest.objects.filter(
                reviewer=entry.reviewer,
                proposal=proposal,
                status__in=["pending", "confirmed"],
            ).exists()

            if has_coi:
                continue

            suggestions.append(
                {
                    "pool_entry_uuid": str(entry.uuid),
                    "reviewer_name": entry.reviewer.user.full_name
                    if entry.reviewer
                    else entry.invited_email,
                    "reviewer_email": entry.reviewer.user.email
                    if entry.reviewer
                    else entry.invited_email,
                    "affinity_score": affinity.affinity_score if affinity else None,
                    "current_assignments": entry.current_assignments,
                    "max_assignments": entry.max_assignments,
                }
            )

        # Sort by affinity score
        suggestions.sort(key=lambda x: x.get("affinity_score") or 0, reverse=True)

        return response.Response(
            {"suggestions": suggestions},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Reassign this item to a different reviewer.",
        request=serializers.ReassignItemSerializer,
        responses={200: serializers.ReassignItemResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def reassign(self, request, uuid=None):
        """Reassign a declined item to a different reviewer."""
        item: models.AssignmentItem = self.get_object()

        if item.status not in [
            models.AssignmentItemStatuses.DECLINED,
            models.AssignmentItemStatuses.EXPIRED,
        ]:
            return response.Response(
                {"error": _("Only declined or expired items can be reassigned.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ReassignItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pool_entry_uuid = serializer.validated_data["reviewer_pool_entry_uuid"]
        manager_notes = serializer.validated_data.get("manager_notes", "")

        try:
            new_pool_entry = models.CallReviewerPool.objects.get(
                uuid=pool_entry_uuid,
                call=item.batch.call,
                invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            )
        except models.CallReviewerPool.DoesNotExist:
            return response.Response(
                {"error": _("Pool entry not found or not eligible for assignments.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if this reviewer already has this proposal assigned
        existing = models.AssignmentItem.objects.filter(
            batch__reviewer_pool_entry=new_pool_entry,
            proposal=item.proposal,
        ).exists()

        if existing:
            return response.Response(
                {"error": _("This reviewer already has this proposal assigned.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Find or create a draft batch for the new reviewer
        # Only reuse MANUAL draft batches to avoid bundling reassignments
        # with algorithm-generated batches pending approval
        batch, created = models.AssignmentBatch.objects.get_or_create(
            call=item.batch.call,
            reviewer_pool_entry=new_pool_entry,
            status=models.AssignmentBatchStatuses.DRAFT,
            source=models.AssignmentSources.MANUAL,
            defaults={
                "created_by": request.user,
                "manager_notes": manager_notes,
            },
        )

        # Get affinity score
        affinity = models.ReviewerProposalAffinity.objects.filter(
            reviewer=new_pool_entry.reviewer,
            proposal=item.proposal,
        ).first()

        # Check for blocking COI
        coi_records = models.ConflictOfInterest.objects.filter(
            reviewer=new_pool_entry.reviewer,
            proposal=item.proposal,
            status__in=["pending", "recused"],
        )
        has_coi = coi_records.exists()

        # Create new assignment item
        new_item = models.AssignmentItem.objects.create(
            batch=batch,
            proposal=item.proposal,
            affinity_score=affinity.affinity_score if affinity else None,
            reassigned_from=item,
            reassign_count=item.reassign_count + 1,
            has_coi=has_coi,
            status=models.AssignmentItemStatuses.COI_BLOCKED
            if has_coi
            else models.AssignmentItemStatuses.PENDING,
        )
        if has_coi:
            new_item.coi_records.set(coi_records)

        # Mark original item as reassigned
        item.status = models.AssignmentItemStatuses.REASSIGNED
        item.save(update_fields=["status"])

        return response.Response(
            {
                "detail": _("Assignment reassigned successfully."),
                "new_item_uuid": str(new_item.uuid),
                "new_batch_uuid": str(batch.uuid),
                "has_coi": has_coi,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Force-unblock a COI-blocked assignment item (manager override).",
        request=serializers.ForceUnblockSerializer,
        responses={200: serializers.AssignmentItemSerializer},
    )
    @decorators.action(detail=True, methods=["post"], url_path="force-unblock")
    def force_unblock(self, request, uuid=None):
        """Force-unblock a COI-blocked assignment item with a reason."""
        item: models.AssignmentItem = self.get_object()

        if item.status != models.AssignmentItemStatuses.COI_BLOCKED:
            return response.Response(
                {"error": _("Only COI-blocked items can be force-unblocked.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.ForceUnblockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        item.status = models.AssignmentItemStatuses.PENDING
        item.has_coi = False
        item.override_reason = serializer.validated_data["override_reason"]
        item.overridden_by = request.user
        item.overridden_at = timezone.now()
        item.save(
            update_fields=[
                "status",
                "has_coi",
                "override_reason",
                "overridden_by",
                "overridden_at",
            ]
        )

        return response.Response(
            serializers.AssignmentItemSerializer(
                item, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    force_unblock_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            ["batch.call", "batch.call.manager"],
        )
    ]


class CallAssignmentConfigurationViewSet(ActionsViewSet):
    """
    ViewSet for managing call assignment configuration.
    """

    lookup_field = "uuid"
    queryset = models.CallAssignmentConfiguration.objects.all()
    serializer_class = serializers.CallAssignmentConfigurationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_staff:
            return queryset

        # Only call managers can view/edit configuration
        connected_calls = get_connected_calls(user)
        return queryset.filter(call__in=connected_calls)


@extend_schema_view(
    retrieve=extend_schema(
        description="Get details of a specific assignment batch with items.",
        responses={200: serializers.MyAssignmentBatchDetailSerializer},
        parameters=[
            OpenApiParameter(
                "uuid",
                type=str,
                location=OpenApiParameter.PATH,
                description="UUID of the assignment batch",
            ),
        ],
    ),
)
class MyAssignmentBatchViewSet(viewsets.ViewSet):
    """
    ViewSet for reviewers to view and respond to their assignment batches.

    This endpoint provides a reviewer-centric view of pending assignments.
    """

    lookup_field = "uuid"
    permission_classes = [rf_permissions.IsAuthenticated]

    @extend_schema(
        description="List all pending assignment batches for the authenticated reviewer.",
        responses={200: serializers.MyAssignmentBatchSerializer(many=True)},
    )
    def list(self, request):
        """List pending assignment batches for the current user."""
        user = request.user

        # Find reviewer profile
        try:
            profile = models.ReviewerProfile.objects.get(user=user)
        except models.ReviewerProfile.DoesNotExist:
            return response.Response([])

        # Get pool entries for this reviewer
        pool_entries = models.CallReviewerPool.objects.filter(reviewer=profile)

        # Get batches that are sent and not expired
        batches = models.AssignmentBatch.objects.filter(
            reviewer_pool_entry__in=pool_entries,
            status=models.AssignmentBatchStatuses.SENT,
        ).order_by("-sent_at")

        result = []
        for batch in batches:
            result.append(
                {
                    "uuid": batch.uuid,
                    "call_uuid": batch.call.uuid,
                    "call_name": batch.call.name,
                    "status": batch.status,
                    "status_display": batch.get_status_display(),
                    "sent_at": batch.sent_at,
                    "expires_at": batch.expires_at,
                    "is_expired": batch.is_expired,
                    "items_count": batch.items.count(),
                    "items_pending_count": batch.items_pending_count,
                    "manager_notes": batch.manager_notes,
                }
            )

        return response.Response(result)

    def retrieve(self, request, uuid=None):
        """Get batch details with items for the current user."""
        user = request.user

        try:
            profile = models.ReviewerProfile.objects.get(user=user)
        except models.ReviewerProfile.DoesNotExist:
            raise exceptions.NotFound(_("Reviewer profile not found."))

        batch = get_object_or_404(
            models.AssignmentBatch,
            uuid=uuid,
            reviewer_pool_entry__reviewer=profile,
        )

        items = []
        for item in batch.items.all():
            items.append(
                {
                    "uuid": item.uuid,
                    "proposal_uuid": item.proposal.uuid,
                    "proposal_name": item.proposal.name,
                    "proposal_slug": item.proposal.slug,
                    "proposal_summary": item.proposal.project_summary or "",
                    "status": item.status,
                    "status_display": item.get_status_display(),
                    "affinity_score": item.affinity_score,
                    "has_coi": item.has_coi,
                }
            )

        result = {
            "uuid": batch.uuid,
            "call_uuid": batch.call.uuid,
            "call_name": batch.call.name,
            "status": batch.status,
            "status_display": batch.get_status_display(),
            "sent_at": batch.sent_at,
            "expires_at": batch.expires_at,
            "is_expired": batch.is_expired,
            "items_count": batch.items.count(),
            "items_pending_count": batch.items_pending_count,
            "manager_notes": batch.manager_notes,
            "items": items,
        }

        return response.Response(result)
