from rest_framework import serializers

from . import models


class SlurmPackageSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.SlurmPackage
        fields = ('uuid', 'url', 'service_settings', 'name', 'cpu_price', 'gpu_price', 'ram_price')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'slurm-package-detail'},
            'service_settings': {'lookup_field': 'uuid', 'read_only': True},
        }
