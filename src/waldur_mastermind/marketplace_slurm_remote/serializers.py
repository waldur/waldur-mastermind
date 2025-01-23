from rest_framework import serializers

from waldur_mastermind.marketplace import models as marketplace_models


class SetLimitsSerializer(serializers.Serializer):
    limits = serializers.JSONField()

    class Meta:
        model = marketplace_models.Resource
        fields = ("limits",)
