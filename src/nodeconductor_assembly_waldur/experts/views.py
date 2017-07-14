from __future__ import unicode_literals

from django.conf import settings
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import decorators, exceptions, permissions, status, response, viewsets
from django_filters.rest_framework import DjangoFilterBackend

from nodeconductor.core import views as core_views
from nodeconductor.structure import filters as structure_filters
from nodeconductor.structure import models as structure_models
from nodeconductor.structure import permissions as structure_permissions
from nodeconductor.users import models as user_models
from nodeconductor.users import tasks as user_tasks
from nodeconductor_assembly_waldur.support import backend as support_backend

from . import serializers, models, filters, tasks


class ExpertProviderViewSet(viewsets.ModelViewSet):
    queryset = models.ExpertProvider.objects.all()
    serializer_class = serializers.ExpertProviderSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExpertProviderFilter


def is_expert_manager(user):
    if user.is_staff:
        return True

    return models.ExpertProvider.objects.filter(
        customer__permissions__is_active=True,
        customer__permissions__user=user,
        customer__permissions__role=structure_models.CustomerRole.OWNER,
    ).exists()


class ExpertRequestViewSet(core_views.ActionsViewSet):
    queryset = models.ExpertRequest.objects.all()
    serializer_class = serializers.ExpertRequestSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExpertRequestFilter
    disabled_actions = ['destroy']

    def get_queryset(self):
        qs = super(ExpertRequestViewSet, self).get_queryset()

        if not is_expert_manager(self.request.user):
            qs = qs.filtered_for_user(self.request.user)
        return qs

    @decorators.list_route()
    def configured(self, request):
        return response.Response(settings.WALDUR_SUPPORT['OFFERINGS'], status=status.HTTP_200_OK)

    @transaction.atomic()
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expert_request = serializer.save()
        support_backend.get_active_backend().create_issue(expert_request.issue)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)


class ExpertBidViewSet(core_views.ActionsViewSet):
    queryset = models.ExpertBid.objects.all()
    serializer_class = serializers.ExpertBidSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExpertBidFilter
    disabled_actions = ['destroy', 'update']

    def is_expert_manager(request, view, obj=None):
        if not is_expert_manager(request.user):
            raise exceptions.PermissionDenied()

    create_permissions = [is_expert_manager]

    def get_queryset(self):
        return super(ExpertBidViewSet, self).get_queryset().filtered_for_user(self.request.user)

    @decorators.detail_route(methods=['post'])
    @transaction.atomic()
    def accept(self, request, uuid=None):
        current_user = self.request.user
        expert_bid = self.get_object()

        expert_request = expert_bid.request
        expert_request.state = models.ExpertRequest.States.ACTIVE
        expert_request.save(update_fields=['state'])

        models.ExpertContract.objects.create(
            request=expert_request,
            team=expert_bid.team,
            price=expert_bid.price,
            description=expert_bid.description,
        )

        for permission in expert_bid.team.permissions.filter(is_active=True):
            invitation = user_models.Invitation.objects.create(
                created_by=current_user,
                email=permission.user.email,
                customer=expert_request.project.customer,
                project=expert_request.project,
                project_role=structure_models.ProjectRole.ADMINISTRATOR,
            )
            user_tasks.send_invitation.delay(invitation.uuid.hex, current_user.full_name or current_user.username)

        for permission in expert_request.project.customer.permissions.all():
            tasks.send_contract.delay(expert_request.uuid.hex, permission.user.email)

        return response.Response({'status': _('Expert bid has been accepted.')}, status=status.HTTP_200_OK)

    def is_pending_request(request, view, obj=None):
        if obj and obj.request.state != models.ExpertRequest.States.PENDING:
            raise exceptions.ValidationError(_('Expert request should be in pending state.'))

    accept_permissions = [structure_permissions.is_owner, is_pending_request]
