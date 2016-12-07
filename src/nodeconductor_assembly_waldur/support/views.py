from nodeconductor.structure import views as structure_views

from . import executors, filters, models, serializers


class IssueViewSet(structure_views.BaseResourcePropertyExecutorViewSet):
    queryset = models.Issue.objects.all()
    serializer_class = serializers.IssueSerializer
    filter_backends = structure_views.BaseResourcePropertyExecutorViewSet.filter_backends + (
        filters.IssueScopeFilterBackend,
    )
    filter_class = filters.IssueFilter
    create_executor = executors.IssueCreateExecutor
    update_executor = executors.IssueUpdateExecutor
    delete_executor = executors.IssueDeleteExecutor
    async_executor = False
