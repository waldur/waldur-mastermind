"""Serializers for the anonymous chat endpoints."""

from rest_framework import serializers

from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.enums import FeedbackCategory


class AnonymousChatStreamRequestSerializer(serializers.Serializer):
    """No ``history`` field — context is reconstructed from DB rows, not client-supplied state."""

    input = serializers.CharField(
        required=True,
        max_length=50000,
        help_text="User input text for the anonymous marketplace assistant.",
    )
    session_id = serializers.CharField(
        required=True,
        min_length=8,
        max_length=64,
        help_text=(
            "Client-generated session identifier. Bound to the originating "
            "IP on first use; subsequent requests from a different IP get "
            "403."
        ),
    )


class AnonymousChatFeedbackRequestSerializer(serializers.Serializer):
    interaction_uuid = serializers.UUIDField(
        required=True,
        help_text="UUID of the interaction the feedback is about (from the streaming `m` frame).",
    )
    feedback_token = serializers.CharField(
        required=True,
        max_length=128,
        help_text="HMAC-bound bearer issued in the streaming `m` frame.",
    )
    score = serializers.IntegerField(
        required=True,
        min_value=-1,
        max_value=1,
        help_text="+1 thumbs-up or -1 thumbs-down (0 not accepted).",
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        default="",
        max_length=500,
        help_text="Optional free-text comment.",
    )
    category = serializers.ChoiceField(
        choices=FeedbackCategory.choices,
        required=False,
        allow_blank=True,
        allow_null=True,
        default="",
        help_text="Required when score == -1; rejected when score == 1.",
    )

    def validate_score(self, value):
        if value not in (-1, 1):
            raise serializers.ValidationError("score must be -1 or 1.")
        return value

    def validate(self, attrs):
        score = attrs.get("score")
        category = attrs.get("category") or ""
        if score == -1 and not category:
            raise serializers.ValidationError(
                {"category": "category is required when score=-1."}
            )
        if score == 1 and category:
            raise serializers.ValidationError(
                {"category": "category is only allowed when score=-1."}
            )
        return attrs


class AnonymousChatClickRequestSerializer(serializers.Serializer):
    """Reuses the same ``feedback_token`` from the streaming frame — no second auth surface."""

    interaction_uuid = serializers.UUIDField(
        required=True,
        help_text="UUID of the interaction this click belongs to.",
    )
    feedback_token = serializers.CharField(
        required=True,
        max_length=128,
        help_text="HMAC-bound bearer issued in the streaming `m` frame (same as /feedback/).",
    )
    offering_uuid = serializers.UUIDField(
        required=True,
        help_text=(
            "UUID of the clicked offering. Must appear in the parent "
            "interaction's recommended set, else 400."
        ),
    )


class AnonymousChatFeedbackSerializer(serializers.ModelSerializer):
    interaction_uuid = serializers.UUIDField(source="interaction.uuid", read_only=True)

    class Meta:
        model = anonymous_models.AnonymousChatFeedback
        fields = [
            "interaction_uuid",
            "score",
            "comment",
            "category",
            "submitted_from_ip",
            "submitted_at",
            "llm_resolution_score",
            "llm_intent_category",
            "llm_hallucination_detected",
            "llm_hallucination_details",
            "llm_summary",
            "llm_reviewed_at",
            "llm_judge_input_tokens",
            "llm_judge_output_tokens",
            "llm_judge_model",
            "modified_at",
        ]
        read_only_fields = fields


class AnonymousChatInteractionSerializer(serializers.ModelSerializer):
    """``feedback`` is nested to avoid a round-trip — the queryset already has ``select_related("feedback")``."""

    feedback = AnonymousChatFeedbackSerializer(read_only=True)
    click_count = serializers.SerializerMethodField(
        help_text="Offering click-throughs on this interaction. Populated by the "
        "by-session transcript, which annotates it; 0 elsewhere."
    )

    def get_click_count(self, obj) -> int:
        # Read the annotation rather than obj.clicks.count() — the latter would
        # issue one query per interaction on every transcript.
        return getattr(obj, "click_count", 0)

    class Meta:
        model = anonymous_models.AnonymousChatInteraction
        fields = [
            "uuid",
            "user_slug",
            "user_input",
            "assistant_blocks",
            "click_count",
            "input_tokens",
            "output_tokens",
            "offering_uuids",
            "result_count",
            "is_flagged",
            "severity",
            "injection_categories",
            "pii_categories",
            "action_taken",
            "warning",
            "ip_address",
            "session_id",
            "last_active_at",
            "created",
            "feedback",
        ]
        read_only_fields = fields


class DailyVolumeSerializer(serializers.Serializer):
    date = serializers.DateField()
    count = serializers.IntegerField()


class SeverityByDaySeriesSerializer(serializers.Serializer):
    NONE = serializers.ListField(child=serializers.IntegerField())
    LOW = serializers.ListField(child=serializers.IntegerField())
    MEDIUM = serializers.ListField(child=serializers.IntegerField())
    HIGH = serializers.ListField(child=serializers.IntegerField())
    CRITICAL = serializers.ListField(child=serializers.IntegerField())


class SeverityByDaySerializer(serializers.Serializer):
    labels = serializers.ListField(child=serializers.DateField())
    series = SeverityByDaySeriesSerializer()


class AnonymousChatKpiResponseSerializer(serializers.Serializer):
    interactions_total = serializers.IntegerField()
    sessions_total = serializers.IntegerField()
    unique_users = serializers.IntegerField(
        help_text="Distinct user_slug values in the window — proxy for active anonymous users."
    )
    flagged_total = serializers.IntegerField()
    feedback_positive = serializers.IntegerField()
    feedback_negative = serializers.IntegerField()
    satisfaction_rate = serializers.FloatField(
        help_text="positive / (positive + negative); null when no human feedback."
    )
    clicks_total = serializers.IntegerField()
    click_through_rate = serializers.FloatField(
        help_text="clicks / interactions; null when no interactions."
    )
    input_tokens_total = serializers.IntegerField(
        help_text=(
            "Prompt tokens summed over the filtered turns. Turns recorded "
            "before per-interaction token capture contribute nothing, so this "
            "understates spend on historical data."
        )
    )
    output_tokens_total = serializers.IntegerField(
        help_text="Completion tokens summed over the filtered turns."
    )

    reviewed_total = serializers.IntegerField(
        help_text=(
            "Threads carrying a judge verdict. Always present, unlike the "
            "review rates below — zero here is the signal that the nightly "
            "pass is off or stalled, so consumers can keep showing the row."
        )
    )
    review_input_tokens_total = serializers.IntegerField(
        help_text=(
            "Prompt tokens spent by the LLM judge. Tracked apart from "
            "``input_tokens_total`` because review runs on its own budget."
        )
    )
    review_output_tokens_total = serializers.IntegerField(
        help_text="Completion tokens spent by the LLM judge."
    )

    # Null when no judge review has run on the filtered set. Consumers branch on ``review_coverage is not None``.
    avg_llm_resolution_score = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text="Mean of llm_resolution_score across reviewed sessions (1-5).",
    )
    llm_intent_distribution = serializers.DictField(
        child=serializers.IntegerField(),
        required=False,
        help_text="Counts keyed by llm_intent_category.",
    )
    hallucination_rate = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text="Share of reviewed sessions flagged as hallucinating.",
    )
    review_coverage = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text=(
            "Reviewed sessions / total reviewable sessions. Operations "
            "health signal — drops below ~90% if the review budget is "
            "too tight or the task is failing."
        ),
    )

    # Time-series & operational aggregates
    daily_volume = DailyVolumeSerializer(
        many=True,
        required=False,
        help_text="Per-day query counts across the filter window.",
    )
    severity_by_day = SeverityByDaySerializer(
        required=False,
        help_text=(
            "Stacked-bar input. Shape: {labels: [iso-date], series: "
            "{NONE: [...], LOW: [...], MEDIUM: [...], HIGH: [...], CRITICAL: [...]}}"
        ),
    )


class AnonymousChatUserAggregateSerializer(serializers.Serializer):
    user_slug = serializers.CharField()
    last_seen = serializers.DateTimeField(allow_null=True)
    total_interactions = serializers.IntegerField()
    session_count = serializers.IntegerField()
    positive_feedback = serializers.IntegerField()
    negative_feedback = serializers.IntegerField()
    no_feedback = serializers.IntegerField()
    injection_strikes = serializers.IntegerField()


class AnonymousChatConversationSerializer(serializers.Serializer):
    """One row per anonymous conversation (session), mirroring the thread table."""

    session_id = serializers.CharField()
    user_slug = serializers.CharField()
    message_count = serializers.IntegerField()
    is_flagged = serializers.BooleanField()
    max_severity = serializers.CharField()
    max_severity_rank = serializers.IntegerField(
        help_text=(
            "Numeric ordinal of max_severity (0=none … 4=critical) for "
            "severity-ordered sorting."
        )
    )
    has_feedback = serializers.BooleanField()
    offerings_shown = serializers.IntegerField()
    offerings_clicked = serializers.IntegerField(
        help_text="Click-throughs on recommended offerings; repeat clicks count separately."
    )
    models_used = serializers.CharField(
        allow_blank=True,
        help_text=(
            "Comma-separated distinct LLM models across the conversation. More "
            "than one when AI_ASSISTANT_MODEL was switched partway through; "
            "blank for conversations predating model tracking."
        ),
    )
    is_reviewed = serializers.BooleanField(
        help_text=(
            "True once the nightly LLM judge has scored this conversation. "
            "One verdict per conversation, recorded on its last turn; a "
            "conversation is never re-judged."
        )
    )
    input_tokens = serializers.IntegerField(
        help_text="Prompt tokens summed over the conversation; 0 for turns predating token tracking."
    )
    output_tokens = serializers.IntegerField(
        help_text="Completion tokens summed over the conversation."
    )
    total_tokens = serializers.IntegerField(
        help_text="input_tokens + output_tokens. Excludes LLM judge spend, which runs on its own budget."
    )
    started = serializers.DateTimeField(allow_null=True)
    last_active = serializers.DateTimeField(allow_null=True)


class AnonymousChatBudgetSnapshotSerializer(serializers.Serializer):
    tokens_today = serializers.IntegerField()
    tokens_limit = serializers.IntegerField()
    resets_at = serializers.DateTimeField()
