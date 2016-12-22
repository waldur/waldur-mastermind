from django.db import transaction
from rest_framework import viewsets, filters as rf_filters, permissions, decorators, response, status, exceptions

from nodeconductor.core import filters as core_filters, views as core_views
from nodeconductor.structure import filters as structure_filters, models as structure_models

from . import filters, models, serializers, backend


class IssueViewSet(viewsets.ModelViewSet):
    queryset = models.Issue.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.IssueSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        permissions.DjangoObjectPermissions,
    )
    filter_backends = (
        structure_filters.GenericRoleFilter,
        core_filters.DjangoMappingFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filter_class = filters.IssueFilter
    serializers = {
        'comment': serializers.CommentSerializer,
    }

    @transaction.atomic()
    def perform_create(self, serializer):
        issue = serializer.save()
        backend.get_active_backend().create_issue(issue)

    @transaction.atomic()
    def perform_update(self, serializer):
        # XXX: It is not right to check for permissions here. This should be moved to upper level.
        #      Permission check should go before validation.
        if not self.request.user.is_staff:
            raise exceptions.PermissionDenied()
        issue = serializer.save()
        backend.get_active_backend().update_issue(issue)

    @transaction.atomic()
    def perform_destroy(self, issue):
        # XXX: It is not right to check for permissions here. This should be moved to upper level.
        #      Permission check should go before validation.
        if not self.request.user.is_staff:
            raise exceptions.PermissionDenied()
        backend.get_active_backend().delete_issue(issue)
        issue.delete()

    def get_serializer_class(self):
        return self.serializers.get(self.action, super(IssueViewSet, self).get_serializer_class())

    def get_serializer_context(self):
        context = super(IssueViewSet, self).get_serializer_context()
        if self.action == 'comment':
            context['issue'] = self.get_object()
        return context

    def _user_has_permission_to_comment(self):
        user = self.request.user
        if user.is_staff:
            return True
        issue = self.get_object()
        if issue.customer and issue.customer.has_user(user, structure_models.CustomerRole.OWNER):
            return True
        if (issue.project and (issue.project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR) or
                               issue.project.has_user(user, structure_models.ProjectRole.MANAGER))):
            return True
        return False

    @decorators.detail_route(methods=['post'])
    def comment(self, request, uuid=None):
        if not self._user_has_permission_to_comment():
            raise exceptions.PermissionDenied()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            comment = serializer.save()
            backend.get_active_backend().create_comment(comment)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)


class CommentViewSet(core_views.UpdateOnlyViewSet):
    queryset = models.Comment.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.CommentSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        permissions.DjangoObjectPermissions,
    )
    filter_backends = (
        structure_filters.GenericRoleFilter,
        rf_filters.DjangoFilterBackend,
    )
    filter_class = filters.CommentFilter

    @transaction.atomic()
    def perform_update(self, serializer):
        comment = serializer.save()
        backend.get_active_backend().update_comment(comment)

    @transaction.atomic()
    def perform_destroy(self, comment):
        backend.get_active_backend().delete_comment(comment)
        comment.delete()


class SupportUserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.SupportUser.objects.all()
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAdminUser,)
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (rf_filters.DjangoFilterBackend,)
    filter_class = filters.SupportUserFilter
