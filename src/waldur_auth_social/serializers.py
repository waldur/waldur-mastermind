from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()


class AuthSerializer(serializers.Serializer):
    client_id = serializers.CharField()
    redirect_uri = serializers.CharField()
    code = serializers.CharField()


class RemoteEduteamsRequestSerializer(serializers.Serializer):
    cuid = serializers.CharField(max_length=256)
