from waldur_core.structure.serializers import BaseResourceSerializer

from . import models


class JobSerializer(BaseResourceSerializer):
    class Meta(BaseResourceSerializer.Meta):
        model = models.Job
