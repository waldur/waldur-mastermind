from waldur_core.structure import filters as structure_filters
from waldur_core.structure import views as structure_views

from . import models, serializers


class TestServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.TestService.objects.all()
    serializer_class = serializers.ServiceSerializer


class TestServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.TestServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer


class TestNewInstanceFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.TestNewInstance


class TestNewInstanceViewSet(structure_views.ResourceViewSet):
    queryset = models.TestNewInstance.objects.all()
    serializer_class = serializers.NewInstanceSerializer
    filter_class = TestNewInstanceFilter

    def perform_create(self, serializer):
        return serializer.save()

    def perform_update(self, serializer):
        return serializer.save()
