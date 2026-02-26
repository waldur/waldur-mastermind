from datetime import datetime

from constance import config
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_mastermind.chat import models
from waldur_mastermind.chat.injection_detection import SeverityLevel


class ChatRequestSerializer(serializers.Serializer):
    input = serializers.CharField(
        required=True,
        max_length=50000,
        help_text="User input text for the chat model.",
    )
    thread_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Existing thread UUID. If omitted, a new thread is created.",
    )
    mode = serializers.ChoiceField(
        choices=models.ChatMode.choices,
        required=False,
        allow_null=True,
        default=None,
        help_text="'reload': replace the last assistant response. 'edit': edit a user message and re-stream. Omit for normal new-message behavior.",
    )
    edit_message_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        default=None,
        help_text="UUID of the user message to edit. Required when mode='edit'.",
    )

    def validate(self, attrs):
        """Validate mode-specific requirements."""
        mode = attrs.get("mode")
        edit_message_uuid = attrs.get("edit_message_uuid")

        if mode and not attrs.get("thread_uuid"):
            raise serializers.ValidationError(
                {"mode": "mode requires thread_uuid to be provided."}
            )
        if mode == models.ChatMode.EDIT and not edit_message_uuid:
            raise serializers.ValidationError(
                {"edit_message_uuid": "edit_message_uuid is required when mode='edit'."}
            )
        if edit_message_uuid and mode != models.ChatMode.EDIT:
            raise serializers.ValidationError(
                {
                    "edit_message_uuid": "edit_message_uuid is only valid with mode='edit'."
                }
            )
        return attrs


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
    e = serializers.CharField(required=False, help_text="Error message.")
    h = serializers.ListField(required=False, help_text="Table headers.")
    r = serializers.ListField(required=False, help_text="Table rows.")
    n = serializers.IntegerField(required=False, help_text="Total row count.")
    m = serializers.DictField(required=False, help_text="System metadata.")


class TokenQuotaUsageResponseSerializer(serializers.ModelSerializer):
    """
    Serializer for TokenQuota showing user's all period limits and usage.
    User limits can be:
    - Null = use system default from constance config
    - -1 = unlimited (no quota enforcement)
    - Non-negative integer = specific token limit

    System defaults are included to provide transparency when user limits are null.
    """

    daily_remaining = serializers.SerializerMethodField()
    weekly_remaining = serializers.SerializerMethodField()
    monthly_remaining = serializers.SerializerMethodField()
    daily_system_default = serializers.SerializerMethodField()
    weekly_system_default = serializers.SerializerMethodField()
    monthly_system_default = serializers.SerializerMethodField()
    # for UI display purposes
    daily_reset_at = serializers.SerializerMethodField()
    weekly_reset_at = serializers.SerializerMethodField()
    monthly_reset_at = serializers.SerializerMethodField()

    class Meta:
        model = models.TokenQuota
        fields = [
            "daily_limit",
            "daily_usage",
            "daily_remaining",
            "daily_reset_at",
            "daily_system_default",
            "weekly_limit",
            "weekly_usage",
            "weekly_remaining",
            "weekly_reset_at",
            "weekly_system_default",
            "monthly_limit",
            "monthly_usage",
            "monthly_remaining",
            "monthly_reset_at",
            "monthly_system_default",
        ]

    def get_daily_remaining(self, obj) -> int | None:
        """Get remaining daily tokens."""
        return obj.get_remaining("daily")

    def get_weekly_remaining(self, obj) -> int | None:
        """Get remaining weekly tokens."""
        return obj.get_remaining("weekly")

    def get_monthly_remaining(self, obj) -> int | None:
        """Get remaining monthly tokens."""
        return obj.get_remaining("monthly")

    def get_daily_system_default(self, obj) -> int:
        """Get system default daily token limit from constance config."""
        return config.LLM_TOKEN_LIMIT_DAILY

    def get_weekly_system_default(self, obj) -> int:
        """Get system default weekly token limit from constance config."""
        return config.LLM_TOKEN_LIMIT_WEEKLY

    def get_monthly_system_default(self, obj) -> int:
        """Get system default monthly token limit from constance config."""
        return config.LLM_TOKEN_LIMIT_MONTHLY

    def get_daily_reset_at(self, obj) -> datetime:
        """Calculate next midnight (00:00:00)."""
        return models.TokenQuota.calculate_next_reset("daily")

    def get_weekly_reset_at(self, obj) -> datetime:
        """Calculate next Monday at midnight."""
        return models.TokenQuota.calculate_next_reset("weekly")

    def get_monthly_reset_at(self, obj) -> datetime:
        """Calculate first day of next month at midnight."""
        return models.TokenQuota.calculate_next_reset("monthly")


class SetTokenQuotaSerializer(serializers.Serializer):
    """
    Serializer for setting token quota limits for a specific user.

    Allows staff/support to configure daily, weekly, and monthly token limits.
    - Null = use system default from constance config
    - -1 = unlimited (no quota enforcement)
    - Non-negative integer = specific token limit
    """

    user_uuid = serializers.UUIDField(
        required=True,
        help_text="UUID of the user to set quota for.",
    )
    daily_limit = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=-1,
        help_text="Daily token limit. Omit or null = system default, -1 = unlimited.",
    )
    weekly_limit = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=-1,
        help_text="Weekly token limit. Omit or null = system default, -1 = unlimited.",
    )
    monthly_limit = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=-1,
        help_text="Monthly token limit. Omit or null = system default, -1 = unlimited.",
    )


class MessageSerializer(serializers.ModelSerializer):
    thread = serializers.SlugRelatedField(slug_field="uuid", read_only=True)
    replaces = serializers.SlugRelatedField(
        slug_field="uuid",
        read_only=True,
    )
    injection_severity = serializers.SerializerMethodField()

    class Meta:
        model = models.Message
        fields = (
            "uuid",
            "thread",
            "role",
            "content",
            "sequence_index",
            "replaces",
            "created",
            "is_flagged",
            "injection_score",
            "injection_severity",
            "injection_categories",
        )
        read_only_fields = (
            "uuid",
            "created",
            "sequence_index",
            "role",
            "replaces",
            "is_flagged",
            "injection_score",
            "injection_severity",
            "injection_categories",
        )

    @extend_schema_field(serializers.ChoiceField(choices=SeverityLevel.choices()))
    def get_injection_severity(self, obj) -> str:
        return SeverityLevel.from_score(obj.injection_score).value

    def get_fields(self):
        fields = super().get_fields()
        # Schema generation: show all fields for OpenAPI docs
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields
        request = self.context.get("request")
        if request and not (request.user.is_staff or request.user.is_support):
            for field_name in (
                "is_flagged",
                "injection_score",
                "injection_severity",
                "injection_categories",
            ):
                fields.pop(field_name, None)
        return fields


class ThreadSessionSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    chat_session = serializers.SlugRelatedField(slug_field="uuid", read_only=True)

    message_count = serializers.IntegerField(read_only=True)
    user_username = serializers.CharField(
        source="chat_session.user.username", read_only=True
    )
    user_full_name = serializers.CharField(
        source="chat_session.user.full_name", read_only=True
    )
    is_flagged = serializers.SerializerMethodField()
    max_severity = serializers.SerializerMethodField()

    class Meta:
        model = models.ThreadSession
        fields = (
            "uuid",
            "name",
            "chat_session",
            "flags",
            "is_archived",
            "message_count",
            "is_flagged",
            "max_severity",
            "user_username",
            "user_full_name",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "created",
            "modified",
            "chat_session",
            "flags",
            "message_count",
            "user_username",
            "user_full_name",
        )

    def get_is_flagged(self, obj) -> bool:
        return obj.flags.get("is_flagged", False)

    @extend_schema_field(serializers.ChoiceField(choices=SeverityLevel.choices()))
    def get_max_severity(self, obj) -> str:
        score = obj.flags.get("max_injection_score", 0.0)
        return SeverityLevel.from_score(score).value


class ChatSessionSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    user = serializers.SlugRelatedField(slug_field="uuid", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = models.ChatSession
        fields = (
            "uuid",
            "user",
            "user_username",
            "user_full_name",
            "created",
            "modified",
        )
        read_only_fields = ("uuid", "user", "created", "modified")
