from rest_framework import serializers


class ChatRequestSerializer(serializers.Serializer):
    input = serializers.CharField(
        required=True, help_text="User input text for the chat model."
    )


class ToolExecuteSerializer(serializers.Serializer):
    tool = serializers.CharField(
        required=True, max_length=100, help_text="Name of the tool to execute."
    )
    arguments = serializers.JSONField(default=dict, help_text="Tool arguments.")


class ChatResponseSerializer(serializers.Serializer):
    """
    NDJSON streaming response format for chat messages.

    Uses single-character keys for bandwidth optimization. Each line is a JSON object
    containing one or more of these fields:

    - k: Component key (markdown, code, table, mermaid, load)
    - c: Content payload (text)
    - t: Type/tag (language for code blocks, component for loading)
    - h: Table headers (array of strings)
    - r: Table rows (array of arrays)
    - n: Row count (number)
    - m: Metadata (object with additional info like token counts)
    - e: Error message (string)

    Examples:
        {"k":"markdown","c":"Hello!"}
        {"k":"code","c":"print('hi')","t":"python"}
        {"k":"table","h":["Name","State"],"r":[["VM1","OK"]],"n":1}
        {"m":{"tokens":150}}
        {"e":"Request failed"}
    """

    k = serializers.CharField(
        required=False, help_text="Component Alias (e.g. 'markdown', 'code', 'table')."
    )
    c = serializers.CharField(required=False, help_text="Content payload.")
    t = serializers.CharField(
        required=False, help_text="Tag or language for dynamic blocks."
    )
    h = serializers.ListField(required=False, help_text="Table headers.")
    r = serializers.ListField(required=False, help_text="Table rows.")
    n = serializers.IntegerField(required=False, help_text="Total row count.")
    m = serializers.DictField(required=False, help_text="System metadata.")
    e = serializers.CharField(required=False, help_text="Error message.")
