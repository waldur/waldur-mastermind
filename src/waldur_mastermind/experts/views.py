from __future__ import unicode_literals

import collections

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, Http404
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, exceptions, permissions, status, response, viewsets

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views
from waldur_core.users import models as user_models
from waldur_core.users import tasks as user_tasks
from waldur_mastermind.support import backend as support_backend

from . import serializers, models, filters, quotas, tasks


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
            link_template=settings.WALDUR_EXPERTS['INVITATION_LINK_TEMPLATE'],
        )
        username = current_user.full_name or current_user.username
        user_tasks.send_invitation.delay(invitation.uuid.hex, username)


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

        if self.request.user.is_staff:
            return qs
        elif is_expert_manager(self.request.user):
            qs = qs.filtered_for_manager(self.request.user)
        else:
            qs = qs.filtered_for_user(self.request.user)

        return qs

    @decorators.list_route()
    def configured(self, request):
        return response.Response(settings.WALDUR_EXPERTS['CONTRACT'], status=status.HTTP_200_OK)

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

    @decorators.detail_route()
    def users(self, request, uuid=None):
        """
        Returns dict, where key is user UUID, and value is list of roles: staff, support, owner, expert.

        1. If user is an owner of organization that has issued current expert request -> owner.
        2. If user is from an organization that has submitted a bid -> expert.
        3. If user is staff/support -> staff, support.

        For example:
        {
            "0b02d56ebb0d4c6cb00a0728b5d9f349": ["owner"],
            "77020e89a4c54b189d0b27a2a863824f": ["expert"],
            "3d8ddca6c44a4cbda9a49e7c7cc1099b": ["staff", "support"]
        }
        """
        expert_request = self.get_object()
        roles = collections.defaultdict(list)

        if expert_request.issue:
            authors = list(expert_request.issue.comments.values_list('author__user__id', flat=True))
            users = get_user_model().objects.filter(id__in=authors)

            owners = expert_request.customer.get_owners()
            owners = [key.hex for key in owners.values_list('uuid', flat=True)]

            teams = list(expert_request.bids.values_list('team_id', flat=True))
            experts = get_user_model().objects.filter(
                projectpermission__project__in=teams,
                projectpermission__is_active=True
            )
            experts = [key.hex for key in experts.values_list('uuid', flat=True)]

            for user in users:
                key = user.uuid.hex
                if user.is_staff:
                    roles[key].append('staff')
                if user.is_support:
                    roles[key].append('support')
                if key in owners:
                    roles[key].append('owner')
                if key in experts:
                    roles[key].append('expert')

        return response.Response(status=status.HTTP_200_OK, data=dict(roles))

    @decorators.detail_route()
    def pdf(self, request, uuid=None):
        expert_request = self.get_object()

        try:
            contract = models.ExpertContract.objects.get(request=expert_request)
            if not contract.has_file():
                raise Http404()

            pdf_file = contract.get_file()
            file_response = HttpResponse(pdf_file, content_type='application/pdf')
            filename = contract.get_filename()
            file_response['Content-Disposition'] = 'attachment; filename="{filename}"'.format(filename=filename)
            return file_response
        except models.ExpertContract.DoesNotExist:
            raise Http404()

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
    disabled_actions = ['update']

    def is_expert_manager(request, view, obj=None):
        if not is_expert_manager(request.user):
            raise exceptions.PermissionDenied()

    create_permissions = [is_expert_manager]

    def get_queryset(self):
        return super(ExpertBidViewSet, self).get_queryset() \
            .filtered_for_user(self.request.user).distinct()

    @decorators.detail_route(methods=['post'])
    @transaction.atomic()
    def accept(self, request, uuid=None):
        current_user = self.request.user
        expert_bid = self.get_object()

        expert_request = expert_bid.request
        expert_request.state = models.ExpertRequest.States.ACTIVE
        expert_request.save(update_fields=['state'])

        expert_contract = models.ExpertContract.objects.create(
            request=expert_request,
            team=expert_bid.team,
            price=expert_bid.price,
            description=expert_bid.description,
        )

        transaction.on_commit(lambda:
                              create_team_invitations(expert_bid.team, expert_request.project, current_user))

        transaction.on_commit(lambda: tasks.create_pdf_contract.delay(expert_contract.id))

        return response.Response({'status': _('Expert bid has been accepted.')}, status=status.HTTP_200_OK)

    def is_pending_request(request, view, obj=None):
        if obj and obj.request.state != models.ExpertRequest.States.PENDING:
            raise exceptions.ValidationError(_('Expert request should be in pending state.'))

    accept_permissions = [structure_permissions.is_owner, is_pending_request]

    def can_delete_bid(request, view, obj=None):
        if not obj:
            return
        if request.user.is_staff:
            return
        if not structure_permissions._has_owner_access(request.user, obj.team.customer):
            raise exceptions.PermissionDenied()

    destroy_permissions = [is_pending_request, can_delete_bid]


structure_views.ProjectCountersView.register_counter('experts', quotas.get_experts_count)
structure_views.CustomerCountersView.register_counter('experts', quotas.get_experts_count)


def experts_customers(user):
    connected_customers_query = structure_models.Customer.objects.all()
    connected_customers_query = connected_customers_query.filter(
        Q(permissions__user=user, permissions__is_active=True) |
        Q(projects__permissions__user=user, projects__permissions__is_active=True)
    ).distinct()

    expert_bids = models.ExpertBid.objects.filter(
        Q(request__customer__in=connected_customers_query) |
        Q(team__customer__in=connected_customers_query)
    ).distinct()

    relation_customers = structure_models.Customer.objects.filter(
        Q(projects__expertbid__in=expert_bids) |
        Q(expertrequest__bids__in=expert_bids)).distinct()

    return Q(customerpermission__customer__in=relation_customers, customerpermission__is_active=True) | \
        Q(projectpermission__project__customer__in=relation_customers, projectpermission__is_active=True)


structure_filters.UserFilterBackend.register_extra_query(experts_customers)
