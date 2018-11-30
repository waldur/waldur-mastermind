from __future__ import unicode_literals

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, views, permissions, decorators, response, status, exceptions as rf_exceptions

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import metadata as structure_metadata
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views

from . import filters, models, serializers, backend, exceptions


class CheckExtensionMixin(core_views.CheckExtensionMixin):
    extension_name = 'WALDUR_SUPPORT'


class IssueViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Issue.objects.all()
    lookup_field = 'uuid'
    filter_backends = (
        filters.IssueCallerOrRoleFilterBackend,
        DjangoFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filter_class = filters.IssueFilter
    serializer_class = serializers.IssueSerializer

    def is_staff_or_support(request, view, obj=None):
        if not request.user.is_staff and not request.user.is_support:
            raise rf_exceptions.PermissionDenied()

    def check_related_resources(request, view, obj=None):
        if obj and obj.offering_set.exists():
            raise rf_exceptions.ValidationError(_('Issue has offering. Please remove it first.'))

    def can_create_user(request, view, obj=None):
        if not request.user.email:
            raise rf_exceptions.ValidationError(_('Current user does not have email, '
                                                  'therefore he is not allowed to create issues.'))

        if not request.user.full_name:
            raise rf_exceptions.ValidationError(_('Current user does not have full_name, '
                                                  'therefore he is not allowed to create issues.'))

    @transaction.atomic()
    def perform_create(self, serializer):
        issue = serializer.save()
        try:
            backend.get_active_backend().create_issue(issue)
        except exceptions.SupportUserInactive:
            raise rf_exceptions.ValidationError({'caller': _('Caller is inactive.')})

    create_permissions = [can_create_user]

    @transaction.atomic()
    def perform_update(self, serializer):
        issue = serializer.save()
        backend.get_active_backend().update_issue(issue)

    update_permissions = partial_update_permissions = [is_staff_or_support]

    @transaction.atomic()
    def perform_destroy(self, issue):
        backend.get_active_backend().delete_issue(issue)
        issue.delete()

    destroy_permissions = [is_staff_or_support, check_related_resources]

    def _comment_permission(request, view, obj=None):
        user = request.user
        if user.is_staff or user.is_support or not obj:
            return
        issue = obj
        if issue.customer and issue.customer.has_user(user, structure_models.CustomerRole.OWNER):
            return
        if (issue.project and (issue.project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR) or
                               issue.project.has_user(user, structure_models.ProjectRole.MANAGER))):
            return
        raise rf_exceptions.PermissionDenied()

    @decorators.detail_route(methods=['post'])
    def comment(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            comment = serializer.save()
            backend.get_active_backend().create_comment(comment)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    comment_serializer_class = serializers.CommentSerializer
    comment_permissions = [_comment_permission]


class CommentViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    serializer_class = serializers.CommentSerializer
    filter_backends = (
        filters.CommentIssueCallerOrRoleFilterBackend,
        DjangoFilterBackend,
        filters.CommentIssueResourceFilterBackend,
    )
    filter_class = filters.CommentFilter
    queryset = models.Comment.objects.all()

    @transaction.atomic()
    def perform_update(self, serializer):
        comment = serializer.save()
        backend.get_active_backend().update_comment(comment)

    update_permissions = partial_update_permissions = [structure_permissions.is_staff]

    @transaction.atomic()
    def perform_destroy(self, comment):
        backend.get_active_backend().delete_comment(comment)
        comment.delete()

    destroy_permissions = [structure_permissions.is_staff]

    def get_queryset(self):
        queryset = super(CommentViewSet, self).get_queryset()

        if not self.request.user.is_staff:
            subquery = Q(is_public=True) | Q(author__user=self.request.user)
            queryset = queryset.filter(subquery)

        return queryset


class IsStaffOrSupportUser(permissions.BasePermission):
    """
    Allows access only to staff or global support users.
    """

    def has_permission(self, request, view):
        return request.user.is_staff or request.user.is_support


class SupportUserViewSet(CheckExtensionMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.SupportUser.objects.all()
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated, IsStaffOrSupportUser,)
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (DjangoFilterBackend,)
    filter_class = filters.SupportUserFilter


class WebHookReceiverView(CheckExtensionMixin, views.APIView):
    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.WebHookReceiverSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(status=status.HTTP_200_OK)


class OfferingViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Offering.objects.all()
    serializer_class = serializers.OfferingSerializer
    lookup_field = 'uuid'
    metadata_class = structure_metadata.ActionsMetadata
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filter_class = filters.OfferingFilter

    @decorators.list_route()
    def configured(self, request):
        summary_config = {}
        for template in models.OfferingTemplate.objects.all():
            summary_config[template.name] = template.config
        return response.Response(summary_config, status=status.HTTP_200_OK)

    @transaction.atomic()
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering = serializer.save()
        backend.get_active_backend().create_issue(offering.issue)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_serializer_class = serializers.OfferingCreateSerializer
    create_permissions = [structure_permissions.is_owner,
                          structure_permissions.is_manager,
                          structure_permissions.is_administrator]

    def offering_is_in_requested_state(offering):
        if offering.state != models.Offering.States.REQUESTED:
            raise rf_exceptions.ValidationError(_('Offering must be in requested state.'))

    @decorators.detail_route(methods=['post'])
    def complete(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response({'status': _('Offering is marked as completed.')}, status=status.HTTP_200_OK)

    complete_validators = [offering_is_in_requested_state]
    complete_permissions = [structure_permissions.is_staff]
    complete_serializer_class = serializers.OfferingCompleteSerializer

    @decorators.detail_route(methods=['post'])
    def terminate(self, request, uuid=None):
        offering = self.get_object()
        offering.state = models.Offering.States.TERMINATED
        offering.terminated_at = timezone.now()
        offering.save()
        return response.Response({'status': _('Offering is marked as terminated.')}, status=status.HTTP_200_OK)

    terminate_permissions = [structure_permissions.is_staff]

    update_permissions = partial_update_permissions = [structure_permissions.is_staff]

    destroy_permissions = [structure_permissions.is_staff]
    destroy_validators = [core_validators.StateValidator(models.Offering.States.TERMINATED)]


class AttachmentViewSet(CheckExtensionMixin,
                        core_views.ActionsViewSet):
    queryset = models.Attachment.objects.all()
    filter_class = filters.AttachmentFilter
    filter_backends = [DjangoFilterBackend]
    serializer_class = serializers.AttachmentSerializer
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']

    @transaction.atomic()
    def perform_destroy(self, attachment):
        backend.get_active_backend().delete_attachment(attachment)
        attachment.delete()

    @transaction.atomic()
    def perform_create(self, serializer):
        attachment = serializer.save()
        backend.get_active_backend().create_attachment(attachment)

    def get_queryset(self):
        queryset = super(AttachmentViewSet, self).get_queryset()

        if not self.request.user.is_staff:
            user_customers = structure_models.Customer.objects.filter(
                permissions__role=structure_models.CustomerRole.OWNER,
                permissions__user=self.request.user,
                permissions__is_active=True)
            subquery = Q(issue__customer__in=user_customers) | Q(issue__caller=self.request.user)
            queryset = queryset.filter(subquery)

        return queryset


class TemplateViewSet(CheckExtensionMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    queryset = models.Template.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.TemplateSerializer


class OfferingTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    queryset = models.OfferingTemplate.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.OfferingTemplateSerializer


class OfferingPlanViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    queryset = models.OfferingPlan.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.OfferingPlanSerializer


def get_offerings_count(scope):
    return scope.quotas.get(name='nc_offering_count').usage


structure_views.CustomerCountersView.register_counter('offerings', get_offerings_count)
structure_views.ProjectCountersView.register_counter('offerings', get_offerings_count)
