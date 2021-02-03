import logging

from django.db import transaction
from django.db.models import Avg, Count, Q
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators
from rest_framework import exceptions as rf_exceptions
from rest_framework import permissions, response, status, views, viewsets

from waldur_core.core import mixins as core_mixins
from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions

from . import backend, exceptions, executors, filters, models, serializers

logger = logging.getLogger(__name__)


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

    update_permissions = partial_update_permissions = [is_staff_or_support]

    @transaction.atomic()
    def perform_destroy(self, issue):
        backend.get_active_backend().delete_issue(issue)
        issue.delete()

    destroy_permissions = [is_staff_or_support]

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
    queryset = models.Priority.objects.all()
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
    permission_classes = (
        permissions.IsAuthenticated,
        IsStaffOrSupportUser,
    )
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SupportUserFilter


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

    @transaction.atomic()
    def perform_create(self, serializer):
        attachment = serializer.save()
        backend.get_active_backend().create_attachment(attachment)

    def get_queryset(self):
        queryset = super(AttachmentViewSet, self).get_queryset()
        return queryset.filter_for_user(self.request.user)


class TemplateViewSet(CheckExtensionMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    queryset = models.Template.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.TemplateSerializer


class FeedbackViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.Feedback.objects.all()
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
