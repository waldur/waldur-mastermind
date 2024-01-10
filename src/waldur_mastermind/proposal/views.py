from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, exceptions, response, status, viewsets
from rest_framework import permissions as rf_permissions

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.exceptions import (
    IncorrectMethodException,
    IncorrectStateException,
)
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import SYSTEM_CUSTOMER_ROLES
from waldur_core.structure import filters as structure_filters
from waldur_mastermind.marketplace.views import BaseMarketplaceView, PublicViewsetMixin
from waldur_mastermind.proposal import filters, models, serializers


class CallManagingOrganisationViewSet(PublicViewsetMixin, BaseMarketplaceView):
    lookup_field = "uuid"
    queryset = models.CallManagingOrganisation.objects.all().order_by("customer__name")
    serializer_class = serializers.CallManagingOrganisationSerializer
    filterset_class = filters.CallManagingOrganisationFilter


class PublicCallViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "uuid"
    queryset = models.Call.objects.filter(
        state__in=[models.Call.States.ACTIVE, models.Call.States.ARCHIVED]
    ).order_by("created")
    serializer_class = serializers.PublicCallSerializer
    filterset_class = filters.CallFilter
    permission_classes = (rf_permissions.AllowAny,)


class ProtectedCallViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.Call.objects.all().order_by("created")
    serializer_class = serializers.ProtectedCallSerializer
    filterset_class = filters.CallFilter
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    destroy_validators = [core_validators.StateValidator(models.Call.States.DRAFT)]

    @staticmethod
    def _action_list_method(set_name, additional_validators=[]):
        def func(self, request, uuid=None):
            call = self.get_object()
            method = self.request.method

            if method == "POST":
                serializer = self.get_serializer(
                    context=self.get_serializer_context(),
                    data=self.request.data,
                )
                serializer.is_valid(raise_exception=True)

                for validator in additional_validators:
                    getattr(serializer, validator)(call)

                serializer.save(call=call)
                return response.Response(
                    serializer.data,
                    status=status.HTTP_201_CREATED,
                )

            return response.Response(
                self.get_serializer(
                    getattr(call, set_name),
                    context=self.get_serializer_context(),
                    many=True,
                ).data,
                status=status.HTTP_200_OK,
            )

        return func

    @staticmethod
    def _action_detail_method(set_name, delete_validators=[], update_validators=[]):
        def func(self, request, uuid=None, obj_uuid=None):
            call = self.get_object()
            method = self.request.method

            try:
                obj = getattr(call, set_name).get(uuid=obj_uuid)

                if method == "DELETE":
                    [validator(obj) for validator in delete_validators]
                    obj.delete()
                    return response.Response(status=status.HTTP_204_NO_CONTENT)

                if method in ["PUT", "PATCH"]:
                    [validator(obj) for validator in update_validators]

                    serializer = self.get_serializer(
                        obj,
                        context=self.get_serializer_context(),
                        data=self.request.data,
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    return response.Response(serializer.data, status=status.HTTP_200_OK)

                serializer = self.get_serializer(
                    obj, context=self.get_serializer_context()
                )
                return response.Response(serializer.data, status=status.HTTP_200_OK)
            except ObjectDoesNotExist:
                return response.Response(status=status.HTTP_404_NOT_FOUND)

        return func

    @decorators.action(detail=True, methods=["get", "post"])
    def offerings(self, request, uuid=None):
        return self._action_list_method("requestedoffering_set")(self, request, uuid)

    offerings_serializer_class = serializers.RequestedOfferingSerializer

    def offering_detail(self, request, uuid=None, obj_uuid=None):
        return self._action_detail_method(
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
        call.state = models.Call.States.ACTIVE
        call.save()
        return response.Response(
            "Call has been activated.",
            status=status.HTTP_200_OK,
        )

    activate_validators = [core_validators.StateValidator(models.Call.States.DRAFT)]

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
        return self._action_list_method("round_set")(self, request, uuid)

    rounds_serializer_class = serializers.RoundSerializer

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

        return self._action_detail_method(
            "round_set",
            delete_validators=[validate_call_state, validate_existing_of_proposals],
            update_validators=[validate_call_state],
        )(self, request, uuid, obj_uuid)

    round_detail_serializer_class = serializers.RoundSerializer

    @decorators.action(detail=True, methods=["get", "post"])
    def reviewers(self, request, uuid=None):
        return self._action_list_method(
            "callreviewer_set", ["validate_unique_together"]
        )(self, request, uuid)

    reviewers_serializer_class = serializers.ReviewerSerializer

    def reviewer_detail(self, request, uuid=None, obj_uuid=None):
        def update_validator(*args, **kwargs):
            raise IncorrectMethodException()

        return self._action_detail_method(
            "callreviewer_set",
            delete_validators=[],
            update_validators=[update_validator],
        )(self, request, uuid, obj_uuid)

    reviewer_detail_serializer_class = serializers.ReviewerSerializer


class ProposalViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ProposalSerializer
    filterset_class = filters.ProposalFilter

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.Proposal.objects.all().order_by("round__start_time")

        customer_ids = permissions_models.UserRole.objects.filter(
            user=user, is_active=True, role__name__in=SYSTEM_CUSTOMER_ROLES
        ).values_list("object_id", flat=True)
        return models.Proposal.objects.filter(
            Q(round__call__manager__customer__in=customer_ids) | Q(created_by=user)
        )

    def is_creator(request, view, obj=None):
        if not obj:
            return
        user = request.user
        if obj.created_by == user or user.is_staff:
            return
        raise exceptions.PermissionDenied()

    update_permissions = partial_update_permissions = destroy_permissions = [is_creator]
    destroy_validators = [core_validators.StateValidator(models.Proposal.States.DRAFT)]

    @decorators.action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        proposal = self.get_object()
        proposal.state = models.Proposal.States.SUBMITTED
        proposal.save()
        return response.Response(
            "Proposal has been submitted.",
            status=status.HTTP_200_OK,
        )

    submit_validators = [core_validators.StateValidator(models.Proposal.States.DRAFT)]

    submit_permissions = [is_creator]


class ReviewViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.ReviewSerializer
    filterset_class = filters.ReviewFilter
    disabled_actions = [
        "create",
        "destroy",
    ]

    def get_queryset(self):
        user = self.request.user

        if user.is_staff:
            return models.Review.objects.all().order_by("created")

        customer_ids = permissions_models.UserRole.objects.filter(
            user=user, is_active=True, role__name__in=SYSTEM_CUSTOMER_ROLES
        ).values_list("object_id", flat=True)
        return models.Review.objects.filter(
            Q(proposal__round__call__manager__customer__in=customer_ids)
            | Q(reviewer__user=user)
            | Q(state=models.Review.States.SUBMITTED, proposal__created_by=user)
        )

    def is_proposal_submitted(review):
        if review.proposal.state != models.Proposal.States.SUBMITTED:
            raise IncorrectStateException()

    def action_permission_check(request, view, obj=None):
        if not obj:
            return

        user = request.user

        if user.is_staff or obj.reviewer.user == user:
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
    ]
    accept_permissions = (
        reject_permissions
    ) = submit_permissions = update_permissions = partial_update_permissions = [
        action_permission_check
    ]
