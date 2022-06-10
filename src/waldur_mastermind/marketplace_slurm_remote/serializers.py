from rest_framework import serializers

from waldur_slurm import models as slurm_models


class UsernameSerializer(serializers.ModelSerializer):
    class Meta:
        model = slurm_models.Association
        fields = ('username',)


class SetStateSerializer(serializers.Serializer):
    state = serializers.CharField(max_length=18)


class SetBackendIdSerializer(serializers.ModelSerializer):
    class Meta:
        model = slurm_models.Allocation
        fields = ('backend_id',)
