import logging
from datetime import datetime, timedelta

from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone as timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, exceptions, response, status, viewsets
from rest_framework import permissions as rf_permissions

from waldur_core.core import validators as core_validators
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.utils import SubqueryCount
from waldur_core.core.views import (
    ActionMethodMixin,
    ActionsViewSet,
    ReadOnlyActionsViewSet,
)
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions
from waldur_core.structure.managers import get_connected_customers
from waldur_core.structure.models import PROJECT_NAME_LENGTH
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.views import BaseMarketplaceView, PublicViewsetMixin
from waldur_mastermind.proposal import (
    filters,
    models,
    serializers,
    utils,
)
from waldur_mastermind.proposal import (
    permissions as proposal_permissions,
)

from . import log

User = auth.get_user_model()
logger = logging.getLogger(__name__)


class CallManagingOrganisationViewSet(PublicViewsetMixin, BaseMarketplaceView):
    lookup_field = "uuid"
    queryset = models.CallManagingOrganisation.objects.all().order_by("customer__name")
    serializer_class = serializers.CallManagingOrganisationSerializer
    filterset_class = filters.CallManagingOrganisationFilter

    @decorators.action(detail=True)
    def stats(self, request, uuid=None):
        instance = self.get_object()
        now = timezone.now()
        one_week_from_now = now + timedelta(weeks=1)

        open_calls = models.Call.objects.filter(
            state=models.Call.States.ACTIVE, manager=instance
        ).count()
        active_rounds = models.Round.objects.filter(
            cutoff_time__gte=now,
            call__manager=instance,
            call__state=models.Call.States.ACTIVE,
        ).count()
        accepted_proposals = models.Proposal.objects.filter(
            state=models.Proposal.States.ACCEPTED,
            round__call__manager=instance,
            round__call__state=models.Call.States.ACTIVE,
        ).count()
        pending_proposals = models.Proposal.objects.filter(
            state__in=[
                models.Proposal.States.IN_REVISION,
                models.Proposal.States.IN_REVIEW,
                models.Proposal.States.SUBMITTED,
            ],
            round__call__manager=instance,
            round__call__state=models.Call.States.ACTIVE,
        ).count()
        pending_review = models.Review.objects.filter(
            state=models.Review.States.SUBMITTED,
            proposal__round__call__manager=instance,
            proposal__round__call__state=models.Call.States.ACTIVE,
        ).count()

        rounds_closing_in_one_week = models.Round.objects.filter(
            cutoff_time__gte=now,
            cutoff_time__lte=one_week_from_now,
            call__manager=instance,
            call__state=models.Call.States.ACTIVE,
        ).count()

        calls_closing_in_one_week = models.Call.objects.filter(
            state=models.Call.States.ACTIVE,
            round__cutoff_time__gte=now,
            round__cutoff_time__lte=one_week_from_now,
            manager=instance,
        ).count()

        offering_requests_pending = models.RequestedOffering.objects.filter(
            state=models.RequestedOffering.States.REQUESTED,
            call__manager=instance,
            call__state=models.Call.States.ACTIVE,
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


class PublicCallViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "uuid"
    queryset = models.Call.objects.filter(
        state__in=[models.Call.States.ACTIVE, models.Call.States.ARCHIVED]
    ).order_by("created")
    serializer_class = serializers.PublicCallSerializer
    filterset_class = filters.CallFilter
    permission_classes = (rf_permissions.AllowAny,)


class ProtectedCallViewSet(UserRoleMixin, ActionsViewSet, ActionMethodMixin):
    lookup_field = "uuid"
    queryset = models.Call.objects.all().order_by("created")
    serializer_class = serializers.ProtectedCallSerializer
    filterset_class = filters.CallFilter
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    destroy_validators = [core_validators.StateValidator(models.Call.States.DRAFT)]

    @decorators.action(detail=True, methods=["get", "post"])
    def offerings(self, request, uuid=None):
        return self.action_list_method("requestedoffering_set")(self, request, uuid)

    offerings_serializer_class = serializers.RequestedOfferingSerializer

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

    @decorators.action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        call = self.get_object()
        if call.round_set.count() == 0:
            raise exceptions.ValidationError(
                _("Call must have a round to be activated.")
            )
        call.state = models.Call.States.ACTIVE
        call.save()
        return response.Response(
            "Call has been activated.",
            status=status.HTTP_200_OK,
        )

    activate_validators = [
        core_validators.StateValidator(
            models.Call.States.DRAFT, models.Call.States.ARCHIVED
        )
    ]

    @decorators.action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        call = self.get_object()
        call.state = models.Call.States.ARCHIVED
        call.save()
        return response.Response(
            "Call has been archived.",
            status=status.HTTP_200_OK,
        )

    archive_validators = [
        core_validators.StateValidator(
            models.Call.States.DRAFT, models.Call.States.ACTIVE
        )
    ]

    @decorators.action(detail=True, methods=["get", "post"])
    def rounds(self, request, uuid=None):
        # TODO: Will be better move this to method of serializer and add tests.
        call = self.get_object()
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

        return response.Response(
            self.get_serializer(
                call.round_set,
                context=self.get_serializer_context(),
                many=True,
            ).data,
            status=status.HTTP_200_OK,
        )

    rounds_serializer_class = serializers.ProtectedRoundSerializer

    def round_detail(self, request, uuid=None, obj_uuid=None):
        def validate_call_state(call_round):
            if call_round.call.state == models.Call.States.ARCHIVED:
                raise IncorrectStateException()

        def validate_existing_of_proposals(call_round):
            if call_round.proposal_set.exclude(
                state__in=[
                    models.Proposal.States.CANCELED,
                    models.Proposal.States.REJECTED,
                ]
            ).exists():
                raise IncorrectStateException()

        return self.action_detail_method(
            "round_set",
            delete_validators=[validate_call_state, validate_existing_of_proposals],
            update_validators=[validate_call_state],
        )(self, request, uuid, obj_uuid)

    round_detail_serializer_class = serializers.ProtectedRoundSerializer

    @decorators.action(detail=True, methods=["post"])
    def attach_documents(self, request, uuid=None):
        instance = self.get_object()

        documents = request.data.getlist("documents", [])

        for file_data in documents:
            obj, created = models.CallDocument.objects.get_or_create(
                call=instance,
                file=file_data,
            )
            if created:
                instance.documents.add(obj)
                log.event_logger.call.info(
                    f"Attachment for call {instance.name} has been added.",
                    event_type="call_document_added",
                    event_context={"call": instance},
                )
                logger.info(f"Attachment for {instance.name} has been added.")

        return response.Response(
            "Documents attached successfully",
            status=status.HTTP_200_OK,
        )

    attach_documents_serializer_class = serializers.CallDocumentSerializer

    @decorators.action(detail=True, methods=["post"])
    def detach_documents(self, request, uuid=None):
        instance = self.get_object()
        documents = request.data.getlist("documents", [])
        for file_data in documents:
            models.CallDocument.objects.get(
                call=instance,
                uuid=file_data,
            ).delete()
            log.event_logger.call.info(
                f"Attachment for call {instance.name} has been removed.",
                event_type="call_document_removed",
                event_context={"call": instance},
            )
            logger.info(f"Attachment for {instance.name} has been removed.")

        return response.Response(
            "Documents removed successfully",
            status=status.HTTP_200_OK,
        )


class ProposalViewSet(UserRoleMixin, ActionsViewSet, ActionMethodMixin):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalSerializer
    filterset_class = filters.ProposalFilter
    disabled_actions = ["update", "partial_update"]
    model = models.Proposal

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.Proposal.objects.all()

        call_ids = permissions_utils.get_scope_ids(
            user,
            content_type=ContentType.objects.get_for_model(models.Call),
            role=RoleEnum.CALL_MANAGER,
        )

        return models.Proposal.objects.filter(
            Q(round__call__manager__customer__in=get_connected_customers(user))
            | Q(created_by=user)
            | Q(round__call__in=call_ids)
        ).distinct()

    def is_creator(request, view, obj=None):
        if not obj:
            return
        user = request.user
        if obj.created_by == user or user.is_staff:
            return
        raise exceptions.PermissionDenied()

    def is_call_manager(request, view, obj=None):
        if not obj:
            return

        proposal = obj
        user = request.user

        if (
            has_permission(
                request,
                PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
                proposal.round.call,
            )
            or user.is_staff
        ):
            return

        raise exceptions.PermissionDenied()

    destroy_permissions = update_project_details_permissions = [is_creator]

    destroy_validators = update_project_details_validators = [
        core_validators.StateValidator(models.Proposal.States.DRAFT)
    ]

    update_project_details_serializer_class = (
        serializers.ProposalUpdateProjectDetailsSerializer
    )

    @decorators.action(detail=True, methods=["post"])
    def update_project_details(self, request, uuid=None):
        proposal = self.get_object()
        serializer = self.get_serializer(data=request.data, instance=proposal)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return response.Response(status=status.HTTP_200_OK)

    @decorators.action(detail=True, methods=["post"])
    def switch_to_team_verification(self, request, uuid=None):
        proposal = self.get_object()
        proposal.state = models.Proposal.States.TEAM_VERIFICATION
        proposal.save()
        return response.Response(status=status.HTTP_200_OK)

    switch_to_team_verification_validators = [
        core_validators.StateValidator(models.Proposal.States.DRAFT)
    ]

    switch_to_team_verification_permissions = [is_creator]

    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        proposal = self.get_object()
        proposal.state = models.Proposal.States.SUBMITTED
        proposal.save()
        return response.Response(
            "Proposal has been submitted.",
            status=status.HTTP_200_OK,
        )

    submit_validators = [
        core_validators.StateValidator(models.Proposal.States.TEAM_VERIFICATION)
    ]

    submit_permissions = [is_creator]

    def perform_create(self, serializer):
        proposal_round = serializer.validated_data.get("round")
        name = serializer.validated_data.get("name")
        call_prefix = (
            proposal_round.call.backend_id
            if proposal_round.call.backend_id
            else proposal_round.call.name
        )
        project_name = " - ".join(
            [call_prefix, proposal_round.start_time.strftime("%Y-%m-%d"), name]
        )[:PROJECT_NAME_LENGTH]
        project = structure_models.Project.objects.create(
            customer=proposal_round.call.manager.customer,
            name=project_name,
        )
        serializer.save(project=project)

    @decorators.action(detail=True, methods=["get", "post"])
    def resources(self, request, uuid=None):
        return self.action_list_method("requestedresource_set")(self, request, uuid)

    resources_serializer_class = serializers.RequestedResourceSerializer

    def resource_detail(self, request, uuid=None, obj_uuid=None):
        def validate_proposal_state(requested_resource):
            if requested_resource.proposal.state != models.Proposal.States.DRAFT:
                raise IncorrectStateException(
                    "Only proposals with a draft status are available for editing."
                )

        return self.action_detail_method(
            "requestedresource_set",
            delete_validators=[validate_proposal_state],
            update_validators=[validate_proposal_state],
        )(self, request, uuid, obj_uuid)

    resource_detail_serializer_class = serializers.RequestedResourceSerializer

    @decorators.action(detail=True, methods=["post"])
    def attach_document(self, request, uuid=None):
        proposal = self.get_object()
        serializer = self.get_serializer(
            context=self.get_serializer_context(),
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(proposal=proposal)

        log.event_logger.proposal.info(
            f"Attachment for proposal {proposal.name} has been added.",
            event_type="proposal_document_added",
            event_context={"proposal": proposal},
        )
        return response.Response(status=status.HTTP_200_OK)

    attach_document_serializer_class = serializers.ProposalDocumentationSerializer

    @decorators.action(detail=True, methods=["post"])
    def allocate(self, request, uuid=None):
        proposal = self.get_object()
        utils.allocate_proposal(proposal)
        proposal.state = models.Proposal.States.ACCEPTED
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal.allocation_comment = serializer.validated_data.get(
            "allocation_comment", ""
        )
        proposal.save()
        return response.Response(
            "Proposal has been allocated.",
            status=status.HTTP_200_OK,
        )

    @decorators.action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        proposal = self.get_object()
        proposal.state = models.Proposal.States.REJECTED
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal.allocation_comment = serializer.validated_data.get(
            "allocation_comment", ""
        )
        proposal.save()
        return response.Response(
            "Proposal has been rejected.",
            status=status.HTTP_200_OK,
        )

    reject_validators = allocate_validators = [
        core_validators.StateValidator(
            models.Proposal.States.IN_REVISION,
            models.Proposal.States.IN_REVIEW,
            models.Proposal.States.SUBMITTED,
        )
    ]
    reject_permissions = allocate_permissions = [
        permission_factory(PermissionEnum.APPROVE_AND_REJECT_PROPOSALS, ["round.call"])
    ]
    reject_serializer_class = allocate_serializer_class = (
        force_approve_serializer_class
    ) = serializers.ProposalAllocateSerializer

    @decorators.action(detail=True, methods=["post"])
    def force_approve(self, request, uuid=None):
        proposal = self.get_object()
        utils.allocate_proposal(proposal)
        proposal.state = models.Proposal.States.ACCEPTED
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposal.allocation_comment = serializer.validated_data.get(
            "allocation_comment", ""
        )
        proposal.save()
        return response.Response(
            "Proposal has been allocated.",
            status=status.HTTP_200_OK,
        )

    force_approve_validators = [
        core_validators.StateValidator(
            models.Proposal.States.SUBMITTED,
            models.Proposal.States.IN_REVIEW,
            models.Proposal.States.REJECTED,
        )
    ]

    force_approve_permissions = [is_call_manager]


class ReviewViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ReviewSerializer
    filterset_class = filters.ReviewFilter
    create_permissions = destroy_permissions = [permissions.is_staff]

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.Review.objects.all().order_by("created")

        return models.Review.objects.filter(
            Q(
                proposal__round__call__manager__customer__in=get_connected_customers(
                    user
                )
            )
            | Q(reviewer=user)
            | Q(state=models.Review.States.SUBMITTED, proposal__created_by=user)
        )

    def is_proposal_submitted(review):
        if review.proposal.state != models.Proposal.States.SUBMITTED:
            raise IncorrectStateException()

    def action_permission_check(request, view, obj: models.Review = None):
        if not obj:
            return

        user = request.user

        if user.is_staff or obj.reviewer == user:
            return

        raise exceptions.PermissionDenied()

    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        review = self.get_object()
        review.state = models.Review.States.IN_REVIEW
        review.save()
        return response.Response(
            "Review has been accepted.",
            status=status.HTTP_200_OK,
        )

    accept_validators = [
        core_validators.StateValidator(models.Review.States.CREATED),
        is_proposal_submitted,
    ]

    @decorators.action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        review = self.get_object()
        review.state = models.Review.States.REJECTED
        review.save()
        return response.Response(
            "Review has been rejected.",
            status=status.HTTP_200_OK,
        )

    reject_validators = [
        core_validators.StateValidator(
            models.Review.States.CREATED, models.Review.States.IN_REVIEW
        ),
        is_proposal_submitted,
    ]

    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        review = self.get_object()
        review.state = models.Review.States.SUBMITTED
        review.save()
        return response.Response(
            "Review has been submitted.",
            status=status.HTTP_200_OK,
        )

    submit_validators = [
        core_validators.StateValidator(models.Review.States.IN_REVIEW),
        is_proposal_submitted,
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

    @decorators.action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        requested_offering = self.get_object()
        requested_offering.state = models.RequestedOffering.States.ACCEPTED
        requested_offering.approved_by = self.request.user
        requested_offering.save()
        return response.Response(
            "The request on offering has been accepted.",
            status=status.HTTP_200_OK,
        )

    accept_validators = [
        core_validators.StateValidator(models.RequestedOffering.States.REQUESTED)
    ]

    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        requested_offering = self.get_object()
        requested_offering.state = models.RequestedOffering.States.CANCELED
        requested_offering.approved_by = self.request.user
        requested_offering.save()
        return response.Response(
            "The request on offering has been canceled.",
            status=status.HTTP_200_OK,
        )

    cancel_validators = [
        core_validators.StateValidator(models.RequestedOffering.States.REQUESTED)
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
    serializer_class = serializers.RoundSerializer
    filterset_class = []
    filter_backends = (DjangoFilterBackend,)

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.Round.objects.all()

        call_ids = permissions_utils.get_scope_ids(
            user,
            content_type=ContentType.objects.get_for_model(models.Call),
            role=RoleEnum.CALL_MANAGER,
        )

        return models.Round.objects.filter(
            Q(call__manager__customer__in=get_connected_customers(user))
            | Q(call__in=call_ids)
        ).distinct()

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
            state=models.Proposal.States.ACCEPTED
        ).values("pk")

        rejected_proposals_subquery = proposals.filter(
            state=models.Proposal.States.REJECTED
        ).values("pk")

        in_review_proposals_subquery = proposals.filter(
            state=models.Proposal.States.IN_REVIEW
        ).values("pk")

        users = users.annotate(
            accepted_proposals=Coalesce(SubqueryCount(accepted_proposals_subquery), 0),
            rejected_proposals=Coalesce(SubqueryCount(rejected_proposals_subquery), 0),
            in_review_proposals=Coalesce(
                SubqueryCount(in_review_proposals_subquery), 0
            ),
        )

        page = self.paginate_queryset(users)
        serializer = serializers.ReviewerSerializer(
            page, many=True, context={"round_obj": round_obj}
        )
        return self.get_paginated_response(serializer.data)
