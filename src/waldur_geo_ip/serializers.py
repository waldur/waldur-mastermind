from rest_framework import serializers


class GeoCodeSerializer(serializers.Serializer):
    address = serializers.CharField(required=True)
