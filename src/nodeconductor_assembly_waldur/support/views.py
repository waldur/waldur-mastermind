from rest_framework import viewsets, filters as rf_filters, permissions, decorators, response, status

from nodeconductor.core import views as core_views
from nodeconductor.structure import filters as structure_filters

from . import filters, models, serializers


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
        rf_filters.DjangoFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filter_class = filters.IssueFilter
    serializers = {
        'comment': serializers.CommentSerializer,
    }

    def get_serializer_class(self):
        return self.serializers.get(self.action, super(IssueViewSet, self).get_serializer_class())

    def get_serializer_context(self):
        context = super(IssueViewSet, self).get_serializer_context()
        if self.action == 'comment':
            context['issue'] = self.get_object()
        return context

    @decorators.detail_route(methods=['post'])
    def comment(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
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
