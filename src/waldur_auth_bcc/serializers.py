from rest_framework import serializers


class UserDetailRequestSerializer(serializers.Serializer):
    civil_number = serializers.RegexField(r'^\d+$')
    tax_number = serializers.RegexField(r'^\d+-\d+$')
