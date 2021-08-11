from waldur_core.structure.views import ResourceViewSet

from . import models, serializers


class JobViewSet(ResourceViewSet):
    queryset = models.Job.objects.all()
    serializer_class = serializers.JobSerializer
