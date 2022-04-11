import logging

from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_freeipa import models as freeipa_models
from waldur_mastermind.marketplace import models, permissions
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_slurm import models as slurm_models
from waldur_slurm import signals as slurm_signals

from . import PLUGIN_NAME, serializers

logger = logging.getLogger(__name__)


class SlurmViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.Resource.objects.filter(offering__type=PLUGIN_NAME)
    serializer_class = marketplace_serializers.ResourceSerializer
    disabled_actions = [
        'retrieve',
        'list',
        'create',
        'update',
        'partial_update',
        'destroy',
    ]

    create_association_serializer_class = (
        delete_association_serializer_class
    ) = serializers.SetUsernameSerializer
    create_association_permissions = delete_association_permissions = [
        permissions.user_is_service_provider_owner_or_service_provider_manager
    ]

    @action(detail=True, methods=['POST'])
    def create_association(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data['username']

        profiles = freeipa_models.Profile.objects.filter(username__iexact=username)
        if not profiles:
            raise ValidationError(
                _(
                    'There is no FreeIPA profile with the given username (case insensitive search).'
                )
            )
        if len(profiles) > 1:
            raise ValidationError(
                _('There are more than one FreeIPA profile with the given username.')
            )

        profile = profiles.first()
        allocation = resource.scope
        if not allocation:
            raise ValidationError(
                _('The resource does not have a related SLURM allocation.')
            )

        association, created = slurm_models.Association.objects.get_or_create(
            allocation=allocation,
            username=username,
        )
        if created:
            logger.info('The association %s has been created', association)
            slurm_signals.slurm_association_created.send(
                slurm_models.Allocation,
                allocation=allocation,
                user=profile.user,
                username=username,
            )
            return Response(
                {
                    'detail': _(
                        'Association between the allocation and the username has been successfully created.'
                    ),
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {
                'detail': _(
                    'Association between the allocation and the username already exists.'
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['POST'])
    def delete_association(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data['username']

        profiles = freeipa_models.Profile.objects.filter(username__iexact=username)
        if not profiles:
            raise ValidationError(
                _(
                    'There is no FreeIPA profile with the given username (case insensitive search).'
                )
            )
        if len(profiles) > 1:
            raise ValidationError(
                _('There are more than one FreeIPA profile with the given username.')
            )

        profile = profiles.first()
        allocation = resource.scope
        if not allocation:
            raise ValidationError(
                _('The resource does not have a related SLURM allocation.')
            )

        associations = slurm_models.Association.objects.filter(
            allocation=allocation, username=username
        )
        if not associations:
            raise ValidationError(
                _('Association between the allocation and the username does not exist.')
            )

        for association in associations:
            association.delete()
            logger.info('The association %s has been deleted', association)

            slurm_signals.slurm_association_deleted.send(
                slurm_models.Allocation, allocation=allocation, user=profile.user
            )

        return Response(
            {
                'detail': _(
                    'Association between the allocation and the username has been successfully deleted.'
                ),
            },
            status=status.HTTP_200_OK,
        )
