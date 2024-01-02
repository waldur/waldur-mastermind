import logging
from datetime import date

from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators
from rest_framework import exceptions as rf_exceptions
from rest_framework import permissions, response, status, views, viewsets
from rest_framework.exceptions import ValidationError

from waldur_core.core import mixins as core_mixins
from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.notifications.models import BroadcastMessage
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend

from . import backend, exceptions, executors, filters, models, serializers

logger = logging.getLogger(__name__)


class CheckExtensionMixin(core_views.ConstanceCheckExtensionMixin):
    extension_name = 'WALDUR_SUPPORT'


class IssueViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Issue.objects.all()
    lookup_field = 'uuid'
    filter_backends = (
        filters.IssueCallerOrRoleFilterBackend,
        DjangoFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filterset_class = filters.IssueFilter
    serializer_class = serializers.IssueSerializer

    def is_staff_or_support(request, view, obj=None):
        if not request.user.is_staff and not request.user.is_support:
            raise rf_exceptions.PermissionDenied()

    def can_create_user(request, view, obj=None):
        if not request.user.email:
            raise rf_exceptions.ValidationError(
                _(
                    'Current user does not have email, '
                    'therefore he is not allowed to create issues.'
                )
            )

        if not request.user.full_name:
            raise rf_exceptions.ValidationError(
                _(
                    'Current user does not have full_name, '
                    'therefore he is not allowed to create issues.'
                )
            )

    @transaction.atomic()
    def perform_create(self, serializer):
        issue = serializer.save()
        try:
            backend.get_active_backend().create_issue(issue)
            backend.get_active_backend().create_confirmation_comment(issue)
        except exceptions.SupportUserInactive:
            raise rf_exceptions.ValidationError({'caller': _('Caller is inactive.')})

    create_permissions = [can_create_user]

    @transaction.atomic()
    def perform_update(self, serializer):
        issue = serializer.save()
        backend.get_active_backend().update_issue(issue)

    def _update_is_available_validator(issue):
        if not backend.get_active_backend().update_is_available(issue):
            raise ValidationError('Updating is not available.')

    update_permissions = partial_update_permissions = [is_staff_or_support]
    update_validators = partial_update_validators = [_update_is_available_validator]

    @transaction.atomic()
    def perform_destroy(self, issue):
        backend.get_active_backend().delete_issue(issue)
        issue.delete()

    def _destroy_is_available_validator(issue):
        if not backend.get_active_backend().destroy_is_available(issue):
            raise ValidationError('Destroying is not available.')

    destroy_permissions = [is_staff_or_support]
    destroy_validators = [_destroy_is_available_validator]

    def _comment_permission(request, view, obj=None):
        user = request.user
        if user.is_staff or user.is_support or not obj:
            return
        issue = obj
        # if it's a personal issue
        if not issue.customer and not issue.project and issue.caller == user:
            return
        if issue.customer and issue.customer.has_user(
            user, structure_models.CustomerRole.OWNER
        ):
            return
        if issue.project and (
            issue.project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR)
            or issue.project.has_user(user, structure_models.ProjectRole.MANAGER)
        ):
            return
        raise rf_exceptions.PermissionDenied()

    @decorators.action(detail=True, methods=['post'])
    def comment(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            comment = serializer.save()
            backend.get_active_backend().create_comment(comment)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    comment_serializer_class = serializers.CommentSerializer
    comment_permissions = [_comment_permission]


class PriorityViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Priority.objects.all().order_by('name')
    serializer_class = serializers.PrioritySerializer
    filterset_class = filters.PriorityFilter
    lookup_field = 'uuid'


class CommentViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    serializer_class = serializers.CommentSerializer
    filter_backends = (
        filters.CommentIssueCallerOrRoleFilterBackend,
        DjangoFilterBackend,
        filters.CommentIssueResourceFilterBackend,
    )
    filterset_class = filters.CommentFilter
    queryset = models.Comment.objects.all()

    @transaction.atomic()
    def perform_update(self, serializer):
        comment = serializer.save()
        backend.get_active_backend().update_comment(comment)

    def _update_is_available_validator(comment):
        if not backend.get_active_backend().comment_update_is_available(comment):
            raise ValidationError('Updating is not available.')

    update_permissions = partial_update_permissions = [structure_permissions.is_staff]
    update_validators = partial_update_validators = [_update_is_available_validator]

    @transaction.atomic()
    def perform_destroy(self, comment):
        backend.get_active_backend().delete_comment(comment)
        comment.delete()

    def _destroy_is_available_validator(comment):
        if not backend.get_active_backend().comment_destroy_is_available(comment):
            raise ValidationError('Comment cannot be destroyed.')

    destroy_permissions = [structure_permissions.is_staff]
    destroy_validators = [_destroy_is_available_validator]

    def get_queryset(self):
        queryset = super().get_queryset()

        if not self.request.user.is_staff:
            subquery = Q(is_public=True) | Q(author__user=self.request.user)
            queryset = queryset.filter(subquery)

        return queryset


class SupportUserViewSet(CheckExtensionMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.SupportUser.objects.all()
    lookup_field = 'uuid'
    permission_classes = (
        permissions.IsAuthenticated,
        structure_permissions.IsStaffOrSupportUser,
    )
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SupportUserFilter


class SupportStatsViewSet(CheckExtensionMixin, views.APIView):
    def get(self, request, format=None):
        today = date.today()
        current_month = today.month
        open_issues_count = (
            models.Issue.objects.exclude(
                status__in=[
                    models.IssueStatus.Types.RESOLVED,
                    models.IssueStatus.Types.CANCELED,
                    'Closed',
                ]
            )
            .filter(resolution_date__isnull=True)
            .count()
        )
        closed_this_month_count = models.Issue.objects.filter(
            status__in=[models.IssueStatus.Types.RESOLVED, 'Closed'],
            resolution_date__month=current_month,
        ).count()

        recent_broadcasts = BroadcastMessage.objects.filter(
            state=BroadcastMessage.States.SENT, created__month=current_month
        )
        recent_broadcasts_count = recent_broadcasts.count()

        data = {
            'open_issues_count': open_issues_count,
            'closed_this_month_count': closed_this_month_count,
            'recent_broadcasts_count': recent_broadcasts_count,
        }

        return JsonResponse(data)


class WebHookReceiverView(CheckExtensionMixin, views.APIView):
    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.WebHookReceiverSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(status=status.HTTP_200_OK)


class AttachmentViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Attachment.objects.all()
    filterset_class = filters.AttachmentFilter
    filter_backends = [DjangoFilterBackend]
    serializer_class = serializers.AttachmentSerializer
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']

    @transaction.atomic()
    def perform_destroy(self, attachment):
        backend.get_active_backend().delete_attachment(attachment)
        attachment.delete()

    def _destroy_is_available_validator(attachment):
        if not backend.get_active_backend().attachment_destroy_is_available(attachment):
            raise ValidationError('Destroying is not available.')

    destroy_validators = [_destroy_is_available_validator]

    @transaction.atomic()
    def perform_create(self, serializer):
        attachment = serializer.save()
        backend.get_active_backend().create_attachment(attachment)

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter_for_user(self.request.user)


class TemplateViewSet(CheckExtensionMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    queryset = models.Template.objects.all().order_by('name')
    lookup_field = 'uuid'
    serializer_class = serializers.TemplateSerializer


class FeedbackViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.Feedback.objects.all().order_by('created')
    disabled_actions = ['update', 'partial_update', 'destroy']
    permission_classes = (core_permissions.ActionsPermission,)
    create_permissions = ()
    create_serializer_class = serializers.CreateFeedbackSerializer
    serializer_class = serializers.FeedbackSerializer
    create_executor = executors.FeedbackExecutor
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.FeedbackFilter

    def is_staff_or_support(request, view, obj=None):
        if not request.user.is_staff and not request.user.is_support:
            raise rf_exceptions.PermissionDenied()

    list_permissions = retrieve_permissions = [is_staff_or_support]


class FeedbackReportViewSet(views.APIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]

    def get(self, request, format=None):
        result = {
            dict(models.Feedback.Evaluation.CHOICES).get(count['evaluation']): count[
                'id__count'
            ]
            for count in models.Feedback.objects.values('evaluation').annotate(
                Count('id')
            )
        }
        return response.Response(result, status=status.HTTP_200_OK)


class FeedbackAverageReportViewSet(views.APIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]

    def get(self, request, format=None):
        avg = models.Feedback.objects.aggregate(Avg('evaluation'))['evaluation__avg']

        if avg:
            result = round(avg, 2)
        else:
            result = None
        return response.Response(result, status=status.HTTP_200_OK)


class ZammadWebHookReceiverView(CheckExtensionMixin, views.APIView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        ticket_id = request.data.get('ticket', {}).get('id')

        if not ticket_id:
            raise ValidationError('Key ticket.id is required.')

        issue: models.Issue = get_object_or_404(models.Issue, backend_id=ticket_id)
        logger.info(
            f'Updating issue {issue.key} based on data from ticket with id {ticket_id}.'
        )
        ZammadServiceBackend().update_waldur_issue_from_zammad(issue)
        ZammadServiceBackend().update_waldur_comments_from_zammad(issue)
        return response.Response(status=status.HTTP_200_OK)
