from waldur_core.structure import filters as structure_filters
from waldur_core.structure import views as structure_views

from . import models, serializers


class TestNewInstanceFilter(structure_filters.BaseResourceFilter):
    __test__ = False

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.TestNewInstance


class TestNewInstanceViewSet(structure_views.ResourceViewSet):
    __test__ = False
    queryset = models.TestNewInstance.objects.all()
    serializer_class = serializers.NewInstanceSerializer
    filterset_class = TestNewInstanceFilter

    def perform_create(self, serializer):
        return serializer.save()

    def perform_update(self, serializer):
        return serializer.save()
