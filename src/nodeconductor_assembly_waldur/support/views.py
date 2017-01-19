from __future__ import unicode_literals

from django.conf import settings
from django.db import transaction
from rest_framework import viewsets, views, filters as rf_filters, permissions, decorators, response, status, exceptions, \
    generics

from nodeconductor.core import filters as core_filters, views as core_views
from nodeconductor.structure import (filters as structure_filters, models as structure_models,
                                     permissions as structure_permissions)

from . import filters, models, serializers, backend


class IssueViewSet(core_views.ActionsViewSet):
    queryset = models.Issue.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.IssueSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        core_filters.DjangoMappingFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filter_class = filters.IssueFilter

    @transaction.atomic()
    def perform_create(self, serializer):
        issue = serializer.save()
        backend.get_active_backend().create_issue(issue)

    @transaction.atomic()
    def perform_update(self, serializer):
        issue = serializer.save()
        backend.get_active_backend().update_issue(issue)

    update_permissions = partial_update_permissions = [structure_permissions.is_staff]

    @transaction.atomic()
    def perform_destroy(self, issue):
        backend.get_active_backend().delete_issue(issue)
        issue.delete()

    destroy_permissions = [structure_permissions.is_staff]

    def _comment_permission(request, view, obj=None):
        user = request.user
        if user.is_staff or not obj:
            return
        issue = obj
        if issue.customer and issue.customer.has_user(user, structure_models.CustomerRole.OWNER):
            return
        if (issue.project and (issue.project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR) or
                               issue.project.has_user(user, structure_models.ProjectRole.MANAGER))):
            return
        raise exceptions.PermissionDenied()

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


class CommentViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    serializer_class = serializers.CommentSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        rf_filters.DjangoFilterBackend,
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
            queryset = queryset.filter(is_public=True)

        return queryset


class SupportUserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.SupportUser.objects.all()
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAdminUser,)
    serializer_class = serializers.SupportUserSerializer
    filter_backends = (rf_filters.DjangoFilterBackend,)
    filter_class = filters.SupportUserFilter


class WebHookReceiverView(views.APIView):
    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.WebHookReceiverSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid(raise_exception=True):
            serializer.save()

        return response.Response(status=status.HTTP_200_OK)


class OfferingListView(views.APIView):

    def get(self, request):
        configuration = settings.WALDUR_SUPPORT['OFFERING']
        return response.Response(configuration, status=status.HTTP_200_OK)


class OfferingView(generics.CreateAPIView):
    serializer_class = serializers.OfferingSerializer
    configuration = settings.WALDUR_SUPPORT['OFFERING']

    def post(self, request, name):
        if name not in self.configuration:
            return response.Response('Provided name "%s" is not registered' % name, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(name=name, data=request.data)
        serializer.is_valid(raise_exception=True)
        issue = serializer.save()
        backend.get_active_backend().create_issue(issue)
        return response.Response(issue.pk, status=status.HTTP_201_CREATED)
