from django.db import transaction

from waldur_core.core.utils import serialize_instance
from waldur_core.structure.views import ResourceViewSet

from . import models, serializers, tasks


class JobViewSet(ResourceViewSet):
    queryset = models.Job.objects.all()
    serializer_class = serializers.JobSerializer

    def perform_create(self, serializer):
        job = serializer.save()
        transaction.on_commit(lambda: tasks.submit_job.delay(serialize_instance(job)))
