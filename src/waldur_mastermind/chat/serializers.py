from rest_framework import serializers


class ChatRequestSerializer(serializers.Serializer):
    input = serializers.CharField(help_text="User input text for the chat model.")


class ChatResponseSerializer(serializers.Serializer):
    k = serializers.CharField(
        required=False, help_text="Component Alias (e.g. 'markdown', 'code')."
    )
    c = serializers.CharField(required=False, help_text="Content payload.")
    t = serializers.CharField(
        required=False, help_text="Tag or language for dynamic blocks."
    )
    m = serializers.DictField(required=False, help_text="System metadata.")
    e = serializers.CharField(required=False, help_text="Error message.")
