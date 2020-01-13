from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics

from waldur_core.structure.filters import GenericRoleFilter

from . import filters, models, serializers


class ChecklistViewset(generics.ListAPIView):
    queryset = models.Checklist.objects.all()
    serializer_class = serializers.ChecklistSerializer


class AnswerViewset(generics.RetrieveUpdateDestroyAPIView):
    queryset = models.Answer.objects.all()
    serializer_class = serializers.AnswerSerializer
    filter_backends = (GenericRoleFilter, DjangoFilterBackend,)
    filterset_class = filters.AnswerFilter
