from rest_framework import serializers


class ChatRequestSerializer(serializers.Serializer):
    input = serializers.CharField(help_text="User input text for the chat model.")
