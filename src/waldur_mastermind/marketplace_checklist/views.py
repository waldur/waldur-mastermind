from django_filters.rest_framework import DjangoFilterBackend
from waldur_core.core.views import ReadOnlyActionsViewSet

from waldur_core.structure.filters import GenericRoleFilter

from . import filters, models, serializers


class ChecklistViewset(ReadOnlyActionsViewSet):
    queryset = models.Checklist.objects.all()
    serializer_class = serializers.ChecklistSerializer


class AnswerViewset(ReadOnlyActionsViewSet):
    queryset = models.Answer.objects.all()
    serializer_class = serializers.AnswerSerializer
    filter_backends = (GenericRoleFilter, DjangoFilterBackend,)
    filterset_class = filters.AnswerFilter
