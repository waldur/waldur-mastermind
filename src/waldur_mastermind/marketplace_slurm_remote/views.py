import logging

from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from django_fsm import TransitionNotAllowed
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_slurm import models as slurm_models
from waldur_slurm import serializers as slurm_serializers

from . import PLUGIN_NAME, serializers

logger = logging.getLogger(__name__)

User = get_user_model()


class SlurmViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = marketplace_models.Resource.objects.filter(
        offering__type=PLUGIN_NAME
    ).exclude(object_id=None)
    serializer_class = marketplace_serializers.ResourceSerializer
    disabled_actions = [
        "retrieve",
        "list",
        "create",
        "update",
        "partial_update",
        "destroy",
    ]

    create_association_serializer_class = delete_association_serializer_class = (
        serializers.UsernameSerializer
    )

    create_association_permissions = delete_association_permissions = (
        set_limits_permissions
    ) = set_usage_permissions = set_state_permissions = set_backend_id_permissions = [
        permissions.user_is_service_provider_owner_or_service_provider_manager
    ]

    @action(detail=True, methods=["post"])
    def set_limits(self, request, uuid=None):
        resource = self.get_object()
        allocation: slurm_models.Allocation = resource.scope

        new_limits = request.data

        if not isinstance(new_limits, dict):
            limits_type = type(new_limits)
            raise ValidationError(
                _("The payload must have dictionary type, not %s." % limits_type)
            )

        old_limits = resource.limits
        resource.limits = request.data
        resource.save(update_fields=["limits"])

        logger.info(
            "The limits for allocation %s have been changed from %s to %s",
            allocation,
            old_limits,
            request.data,
        )

        return Response(
            {"status": _("Limits are successfully set.")},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["POST"])
    def set_usage(self, request, uuid=None):
        resource = self.get_object()
        allocation: slurm_models.Allocation = resource.scope
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        if payload["username"] == "TOTAL_ACCOUNT_USAGE":
            allocation.cpu_usage = payload["cpu_usage"]
            allocation.gpu_usage = payload["gpu_usage"]
            allocation.ram_usage = payload["ram_usage"]
            allocation.save(update_fields=["cpu_usage", "gpu_usage", "ram_usage"])
            logger.info(
                "The total usage for allocation %s has been set: %s.",
                allocation,
                payload,
            )
        else:
            (
                user_usage,
                created,
            ) = slurm_models.AllocationUserUsage.objects.update_or_create(
                allocation=allocation,
                user=payload["user"],
                username=payload["username"],
                month=payload["month"],
                year=payload["year"],
                defaults={
                    "cpu_usage": payload["cpu_usage"],
                    "ram_usage": payload["ram_usage"],
                    "gpu_usage": payload["gpu_usage"],
                },
            )
            if created:
                logger.info(
                    "User usage %s has been created with the following params: %s",
                    user_usage,
                    payload,
                )
            else:
                logger.info(
                    "User usage %s has been updated with the following params: %s",
                    user_usage,
                    payload,
                )

        return Response(
            {
                "detail": _("Allocation usage has been updated successfully."),
            },
            status=status.HTTP_200_OK,
        )

    set_usage_serializer_class = slurm_serializers.AllocationUserUsageCreateSerializer

    @action(detail=True, methods=["POST"])
    def create_association(self, request, uuid=None):
        resource = self.get_object()
        offering = resource.offering
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]

        allocation = resource.scope
        if not allocation:
            raise ValidationError(
                _("The resource does not have a related SLURM allocation.")
            )

        association, created = slurm_models.Association.objects.get_or_create(
            allocation=allocation,
            username=username,
        )
        if created:
            logger.info("The association %s has been created", association)
            offering_users = marketplace_models.OfferingUser.objects.filter(
                offering=offering, username=username
            )
            if offering_users.count() == 0:
                logger.info(
                    "There is no offering user for %s with username %s",
                    offering,
                    username,
                )
            else:
                offering_user = offering_users.first()
                if offering_user.propagation_date is None:
                    offering_user.set_propagation_date()
                    offering_user.save(update_fields=["propagation_date"])
                    logger.info(
                        "The corresponding offering user %s has been marked as propagated",
                        offering_user,
                    )
            return Response(
                {
                    "detail": _(
                        "Association between the allocation and the username has been successfully created."
                    ),
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {
                "detail": _(
                    "Association between the allocation and the username already exists."
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["POST"])
    def delete_association(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]

        allocation = resource.scope
        if not allocation:
            raise ValidationError(
                _("The resource does not have a related SLURM allocation.")
            )

        associations = slurm_models.Association.objects.filter(
            allocation=allocation, username=username
        )
        if not associations:
            raise ValidationError(
                _("Association between the allocation and the username does not exist.")
            )

        for association in associations:
            association.delete()
            logger.info("The association %s has been deleted", association)

        return Response(
            {
                "detail": _(
                    "Association between the allocation and the username has been successfully deleted."
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["POST"])
    def set_state(self, request, uuid=None):
        resource = self.get_object()
        allocation: slurm_models.Allocation = resource.scope
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        state = serializer.validated_data["state"]

        state_to_methods_map = {
            "creating": "begin_creating",
            "updating": "begin_updating",
            "deletion_scheduled": "schedule_deleting",
            "update_scheduled": "schedule_updating",
            "deleting": "begin_deleting",
            "ok": "set_ok",
            "erred": "set_erred",
        }

        transition_method_name = state_to_methods_map.get(state)
        if not transition_method_name:
            raise ValidationError(
                _("Invalid state: a corresponding method for transition is absent")
            )
        try:
            transition_method = getattr(allocation, transition_method_name)
            transition_method()
            allocation.save(update_fields=["state"])
            return Response(
                {
                    "detail": _("Allocation state has been changed to %s" % state),
                },
                status.HTTP_200_OK,
            )
        except TransitionNotAllowed:
            return Response(
                {
                    "detail": _(
                        "Allocation state can not be changed from {} to {}.".format(
                            allocation.state, state
                        )
                    ),
                },
                status.HTTP_409_CONFLICT,
            )

    set_state_serializer_class = serializers.SetStateSerializer

    @action(detail=True, methods=["POST"])
    def set_backend_id(self, request, uuid=None):
        resource = self.get_object()
        allocation: slurm_models.Allocation = resource.scope
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data["backend_id"]
        old_backend_id = allocation.backend_id
        if new_backend_id != old_backend_id:
            allocation.backend_id = serializer.validated_data["backend_id"]
            allocation.save(update_fields=["backend_id"])
            logger.info(
                "%s has changed backend_id from %s to %s",
                request.user.full_name,
                old_backend_id,
                new_backend_id,
            )

            return Response(
                {"status": _("Allocation backend_id has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Allocation backend_id is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_backend_id_serializer_class = serializers.SetBackendIdSerializer
