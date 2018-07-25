from __future__ import unicode_literals

from rest_framework import serializers

from . import models


class AuthResultSerializer(serializers.ModelSerializer):

    token = serializers.SerializerMethodField()

    class Meta:
        model = models.AuthResult
        fields = ('uuid', 'token', 'phone', 'message', 'state', 'error_message', 'details')
        write_only_fields = ('phone', )
        read_only_fields = ('uuid', 'token', 'message', 'state', 'error_message', 'details')

    def get_token(self, auth_result):
        if auth_result.user:
            return auth_result.user.auth_token.key
        return None
