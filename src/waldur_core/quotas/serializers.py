from rest_framework import serializers


class QuotaSerializer(serializers.Serializer):
    name = serializers.CharField()
    usage = serializers.IntegerField()
    limit = serializers.IntegerField()
