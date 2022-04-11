from rest_framework import serializers


class SetUsernameSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=32)
