from __future__ import unicode_literals

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from rest_framework import decorators, exceptions, permissions, status, response, viewsets
from django_filters.rest_framework import DjangoFilterBackend

from nodeconductor.core import views as core_views
from nodeconductor.structure import filters as structure_filters
from nodeconductor.structure import models as structure_models
from nodeconductor.structure import permissions as structure_permissions
from nodeconductor.structure import views as structure_views
from nodeconductor.users import models as user_models
from nodeconductor.users import tasks as user_tasks
from nodeconductor_assembly_waldur.support import backend as support_backend

from . import serializers, models, filters, tasks


def is_expert_manager(user):
    if user.is_staff:
        return True

    return models.ExpertProvider.objects.filter(
        customer__permissions__is_active=True,
        customer__permissions__user=user,
        customer__permissions__role=structure_models.CustomerRole.OWNER,
    ).exists()


def create_team_invitations(team, project, current_user):
    for permission in team.permissions.filter(is_active=True):
        invitation = user_models.Invitation.objects.create(
            created_by=current_user,
            email=permission.user.email,
            customer=project.customer,
            project=project,
            project_role=structure_models.ProjectRole.ADMINISTRATOR,
        )
        user_tasks.send_invitation.delay(invitation.uuid.hex, current_user.full_name or current_user.username)


def cancel_team_invitations(team, project):
    user_emails = [permission.user.email for permission in team.permissions.filter(is_active=True)]
    invitations = user_models.Invitation.objects.filter(
        email__in=user_emails,
        project=project,
        project_role=structure_models.ProjectRole.ADMINISTRATOR,
        state=user_models.Invitation.State.PENDING,
    )
    for invitation in invitations:
        invitation.cancel()


def revoke_request_permissions(expert_request):
    if not hasattr(expert_request, 'contract'):
        return
    team = expert_request.contract.team
    project = expert_request.project
    for permission in team.permissions.filter(is_active=True):
        project.remove_user(permission.user)
    cancel_team_invitations(team, project)


class ExpertProviderViewSet(viewsets.ModelViewSet):
    queryset = models.ExpertProvider.objects.all()
    serializer_class = serializers.ExpertProviderSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExpertProviderFilter


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

    @transaction.atomic()
    @decorators.detail_route(methods=['post'])
    def cancel(self, request, *args, **kwargs):
        expert_request = self.get_object()
        expert_request.state = models.ExpertRequest.States.CANCELLED
        expert_request.save(update_fields=['state'])
        revoke_request_permissions(expert_request)
        return response.Response({'status': _('Expert request has been cancelled.')}, status=status.HTTP_200_OK)

    @transaction.atomic()
    @decorators.detail_route(methods=['post'])
    def complete(self, request, *args, **kwargs):
        expert_request = self.get_object()
        expert_request.state = models.ExpertRequest.States.COMPLETED
        expert_request.save(update_fields=['state'])
        revoke_request_permissions(expert_request)
        return response.Response({'status': _('Expert request has been completed.')}, status=status.HTTP_200_OK)

    def is_valid_request(request, view, obj=None):
        expert_request = obj

        if not expert_request:
            return

        if expert_request.state not in (models.ExpertRequest.States.ACTIVE, models.ExpertRequest.States.PENDING):
            raise exceptions.ValidationError(_('Expert request should be in active or pending state.'))

    def is_owner(request, view, obj=None):
        expert_request = obj

        if not expert_request:
            return

        if not structure_permissions._has_owner_access(request.user, expert_request.project.customer):
            raise exceptions.PermissionDenied()

    cancel_permissions = complete_permissions = [is_owner, is_valid_request]


class ExpertBidViewSet(core_views.ActionsViewSet):
    queryset = models.ExpertBid.objects.all()
    serializer_class = serializers.ExpertBidSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
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

        create_team_invitations(expert_bid.team, expert_request.project, current_user)

        for permission in expert_request.project.customer.permissions.all():
            tasks.send_contract.delay(expert_request.uuid.hex, permission.user.email)

        return response.Response({'status': _('Expert bid has been accepted.')}, status=status.HTTP_200_OK)

    def is_pending_request(request, view, obj=None):
        if obj and obj.request.state != models.ExpertRequest.States.PENDING:
            raise exceptions.ValidationError(_('Expert request should be in pending state.'))

    accept_permissions = [structure_permissions.is_owner, is_pending_request]


def get_project_experts_count(project):
    valid_states = (models.ExpertRequest.States.ACTIVE, models.ExpertRequest.States.PENDING)
    query = Q(project=project, state__in=valid_states)
    return models.ExpertRequest.objects.filter(query).count()


def get_customer_experts_count(customer):
    query = Q(state=models.ExpertRequest.States.PENDING) |\
            Q(state=models.ExpertRequest.States.ACTIVE, contract__team__customer=customer)
    return models.ExpertRequest.objects.filter(query).count()


structure_views.ProjectCountersView.register_counter('experts', get_project_experts_count)
structure_views.CustomerCountersView.register_counter('experts', get_customer_experts_count)
