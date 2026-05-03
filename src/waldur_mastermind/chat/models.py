import logging
from datetime import timedelta
from enum import IntEnum

from constance import config
from django.core.validators import MinValueValidator
from django.db import connection, models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core.models import DescribableMixin, NameMixin, User, UuidMixin
from waldur_mastermind.chat.enums import FeedbackCategory
from waldur_mastermind.chat.input_guards import (
    DetectionAction,
    InputGuardResult,
    SeverityLevel,
)

logger = logging.getLogger(__name__)


class ChatMode(models.TextChoices):
    RELOAD = "reload", _("Reload")
    EDIT = "edit", _("Edit")


class TokenLimit(IntEnum):
    UNLIMITED = -1


class TokenQuota(UuidMixin, TimeStampedModel):
    """
    Manages token quota and usage tracking for AI chat features.
    Supports daily, weekly, and monthly quotas to balance consumption.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="token_quota",
    )

    # Quota limits (null = use system default from constance)
    daily_limit = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-1)],
        help_text=_(
            "Daily token limit (non-negative integer). Null uses system default. -1 means unlimited."
        ),
    )
    weekly_limit = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-1)],
        help_text=_(
            "Weekly token limit (non-negative integer). Null uses system default. -1 means unlimited."
        ),
    )
    monthly_limit = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-1)],
        help_text=_(
            "Monthly token limit (non-negative integer). Null uses system default. -1 means unlimited."
        ),
    )

    # Current period usage (reset when period expires)
    daily_usage = models.PositiveIntegerField(default=0)
    weekly_usage = models.PositiveIntegerField(default=0)
    monthly_usage = models.PositiveIntegerField(default=0)

    daily_reset_last_at = models.DateTimeField(default=timezone.now)
    weekly_reset_last_at = models.DateTimeField(default=timezone.now)
    monthly_reset_last_at = models.DateTimeField(default=timezone.now)

    PERIOD_MAP = {
        "daily": (
            "daily_usage",
            "daily_limit",
            "AI_ASSISTANT_TOKEN_LIMIT_DAILY",
        ),
        "weekly": (
            "weekly_usage",
            "weekly_limit",
            "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY",
        ),
        "monthly": (
            "monthly_usage",
            "monthly_limit",
            "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY",
        ),
    }

    class Meta:
        verbose_name = _("Token Quota")
        verbose_name_plural = _("Token Quotas")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["daily_reset_last_at"]),
            models.Index(fields=["weekly_reset_last_at"]),
            models.Index(fields=["monthly_reset_last_at"]),
        ]

    def __str__(self):
        return f"TokenQuota({self.user.username})"

    @classmethod
    def for_user(cls, user: User, lock: bool = False) -> "TokenQuota":
        """
        Get or create TokenQuota for a user, optionally with a row-level lock.
        Handles concurrent creation attempts safely.
        """
        if lock and not connection.in_atomic_block:
            raise RuntimeError(
                "Locking a quota requires an active transaction.atomic() block."
            )

        if lock:
            # Try to get and lock existing record first
            try:
                return cls.objects.select_for_update().get(user=user)
            except cls.DoesNotExist:
                # Record doesn't exist - create it
                # get_or_create handles race condition if another thread creates it
                quota, _ = cls.objects.get_or_create(user=user)
                # Re-fetch with lock to ensure consistency
                return cls.objects.select_for_update().get(pk=quota.pk)
        else:
            # No lock requested - simple get_or_create
            quota, _ = cls.objects.get_or_create(user=user)
            return quota

    @staticmethod
    def _coerce_limit(value, field_name: str = "limit"):
        if value is None:
            return None

        try:
            limit = int(value)
        except (TypeError, ValueError) as e:
            logger.error(
                f"Invalid token limit configuration: {field_name}={value} (type={type(value).__name__})"
            )
            raise ValueError(
                f"Token limit {field_name} must be an integer, got {type(value).__name__}: {value}"
            ) from e

        if limit < TokenLimit.UNLIMITED:
            logger.error(
                f"Token limit {field_name} is below minimum: {limit} < {TokenLimit.UNLIMITED}"
            )
            raise ValueError(
                f"Token limit {field_name} must be >= {TokenLimit.UNLIMITED} (-1 for unlimited), got {limit}"
            )

        return limit

    def get_effective_limit(self, period: str) -> int:
        """
        Get limit for period, falling back to system defaults.

        System defaults are defined in constance settings. -1 means unlimited.
        User specific limits override system defaults. None means use system default. -1 means unlimited.

        Returns:
        - TokenLimit.UNLIMITED: no quota enforcement
        - Non-negative integer: specific token limit
        """
        if period not in self.PERIOD_MAP:
            raise ValueError(f"Invalid period: {period}")

        _, user_limit_attr, system_default_limit_attr = self.PERIOD_MAP[period]

        # Get user-specific limit first
        user_limit = self._coerce_limit(getattr(self, user_limit_attr), user_limit_attr)
        if user_limit is not None:
            return user_limit  # User-specific limit is set

        system_limit = getattr(config, system_default_limit_attr, TokenLimit.UNLIMITED)
        coerced = self._coerce_limit(system_limit, system_default_limit_attr)

        if coerced is None:
            return TokenLimit.UNLIMITED

        return coerced

    def add_usage(self, tokens: int) -> None:
        """
        Record token usage across all periods.
        """
        if not connection.in_atomic_block:
            raise RuntimeError(
                "This operation requires a transaction. Use transaction.atomic()."
            )

        if tokens < 0:
            raise ValueError(f"Token count must be non-negative, got {tokens}")

        TokenQuota.objects.filter(pk=self.pk).update(
            daily_usage=models.F("daily_usage") + tokens,
            weekly_usage=models.F("weekly_usage") + tokens,
            monthly_usage=models.F("monthly_usage") + tokens,
        )

        self.refresh_from_db(fields=["daily_usage", "weekly_usage", "monthly_usage"])

    def ensure_periods_reset(self) -> None:
        """
        Lazily reset any stale period usage counters.
        """
        if not connection.in_atomic_block:
            raise RuntimeError(
                "ensure_periods_reset requires an active transaction.atomic() block."
            )

        now = timezone.now()
        update_fields = []

        for period in ("daily", "weekly", "monthly"):
            reset_field = f"{period}_reset_last_at"
            usage_field = f"{period}_usage"
            period_start = self.calculate_reset_period_start(period)

            if getattr(self, reset_field) < period_start:
                setattr(self, usage_field, 0)
                setattr(self, reset_field, now)
                update_fields.extend([usage_field, reset_field])

        if update_fields:
            self.save(update_fields=update_fields)

    def get_remaining(self, period: str) -> int | None:
        """
        Get remaining tokens for a period.
        Returns None if unlimited.
        """
        limit = self.get_effective_limit(period)
        if limit == TokenLimit.UNLIMITED:  # Unlimited
            return None

        usage_attr = self.PERIOD_MAP[period][0]
        current_usage = getattr(self, usage_attr)

        return max(0, limit - current_usage)

    @staticmethod
    def calculate_next_reset(period: str):
        from_time = timezone.now()

        if period == "daily":
            # Next midnight
            return (from_time + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        elif period == "weekly":
            # Next Monday at midnight
            days_until_monday = (7 - from_time.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            next_monday = from_time + timedelta(days=days_until_monday)
            return next_monday.replace(hour=0, minute=0, second=0, microsecond=0)

        elif period == "monthly":
            # First day of next month at midnight
            if from_time.month == 12:
                next_month = from_time.replace(year=from_time.year + 1, month=1, day=1)
            else:
                next_month = from_time.replace(month=from_time.month + 1, day=1)
            return next_month.replace(hour=0, minute=0, second=0, microsecond=0)

        else:
            raise ValueError(f"Invalid period: {period}")

    @staticmethod
    def calculate_reset_period_start(period: str):
        now = timezone.now()
        if period == "daily":
            # Start of current day
            return now.replace(hour=0, minute=0, second=0, microsecond=0)

        elif period == "weekly":
            # Start of current week (Monday)
            return (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        elif period == "monthly":
            # First of current month
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        else:
            raise ValueError(f"Invalid period: {period}")


class ChatSession(UuidMixin, TimeStampedModel):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="chat_session"
    )

    class Meta:
        verbose_name = _("Chat session")

    def __str__(self):
        return f"ChatSession({self.user.username})"


class ThreadSession(UuidMixin, TimeStampedModel):
    chat_session = models.ForeignKey(
        ChatSession, on_delete=models.CASCADE, related_name="threads"
    )
    name = models.CharField(_("name"), max_length=150, default="New chat")
    flags = models.JSONField(default=dict, blank=True)
    is_archived = models.BooleanField(default=False)
    cancel_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "Set when user requests stream cancellation; cleared after stream completes."
        ),
    )
    title_gen_input_tokens = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=_("Input tokens consumed by the title generation LLM call."),
    )
    title_gen_output_tokens = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=_("Output tokens consumed by the title generation LLM call."),
    )

    class Meta:
        verbose_name = _("Thread session")
        indexes = [
            models.Index(fields=["chat_session"]),
        ]

    def __str__(self):
        return f"ThreadSession({self.name})"

    def update_detection_flags(self):
        flagged = self.messages.filter(is_flagged=True)
        severities = list(flagged.values_list("severity", flat=True))
        if severities:
            max_sev = max(SeverityLevel(s) for s in severities).value
        else:
            max_sev = SeverityLevel.NONE.value

        self.flags = {
            **self.flags,
            "is_flagged": bool(severities),
            "max_severity": max_sev,
        }
        self.save(update_fields=["flags"])


class Message(UuidMixin, TimeStampedModel):
    class Role(models.TextChoices):
        USER = "user", _("User")
        ASSISTANT = "assistant", _("Assistant")

    thread = models.ForeignKey(
        ThreadSession, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    # Persisted rendering of the assistant/user turn as a list of UI blocks.
    # Mirrors the frontend UIBlock shape so history reload needs no conversion.
    blocks = models.JSONField(default=list)
    # Optional non-blocking warning surfaced by guards (e.g. PII redaction).
    warning = models.TextField(blank=True, default="")
    sequence_index = models.PositiveIntegerField()
    replaces = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replaced_by",
    )

    # Token usage tracking (populated on assistant messages only)
    input_tokens = models.PositiveIntegerField(null=True, blank=True, default=None)
    output_tokens = models.PositiveIntegerField(null=True, blank=True, default=None)

    # Prompt injection detection fields
    is_flagged = models.BooleanField(default=False, db_index=True)
    severity = models.CharField(
        max_length=10,
        choices=SeverityLevel.choices(),
        default=SeverityLevel.NONE.value,
        db_index=True,
    )
    injection_categories = models.JSONField(default=list, blank=True)
    pii_categories = models.JSONField(default=list, blank=True)
    action_taken = models.CharField(
        max_length=10,
        choices=DetectionAction.choices(),
        default=DetectionAction.ALLOW.value,
        blank=True,
        db_index=True,
    )
    feedback_score = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "User feedback: True=thumbs up, False=thumbs down, None=no feedback."
        ),
    )
    feedback_comment = models.TextField(
        null=True,
        blank=True,
        default=None,
        help_text=_("Optional user comment accompanying feedback."),
    )
    feedback_category = models.CharField(
        max_length=32,
        choices=FeedbackCategory.choices,  # type: ignore[arg-type]
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "Category tag when feedback_score is False (thumbs down); null otherwise."
        ),
    )
    feedback_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "Timestamp of the most recent feedback submission; overwritten on resubmit."
        ),
    )

    class Meta:
        ordering = ["sequence_index"]
        verbose_name = _("Message")
        indexes = [
            models.Index(fields=["thread"]),
            models.Index(fields=["replaces"]),
            # Partial index: only rows with submitted feedback. Most messages
            # never get feedback, so a full index would be mostly NULL padding.
            models.Index(
                fields=["feedback_score"],
                condition=models.Q(feedback_score__isnull=False),
                name="chat_msg_fb_score_partial_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "sequence_index"],
                condition=models.Q(replaces__isnull=True),
                name="unique_active_message_sequence",
            ),
        ]

    def __str__(self):
        return f"Message({self.role}, seq={self.sequence_index})"

    def apply_detection_result(self, result: InputGuardResult):
        self.is_flagged = result.is_flagged
        self.severity = result.severity.value
        self.injection_categories = [
            p["category"] for p in result.injection.matched_patterns
        ]
        self.action_taken = result.action.value
        self.pii_categories = [d["entity_type"] for d in result.pii.pii_detections]
        self.save(
            update_fields=[
                "is_flagged",
                "severity",
                "injection_categories",
                "pii_categories",
                "action_taken",
            ]
        )


class SystemPrompt(UuidMixin, NameMixin, DescribableMixin, TimeStampedModel):
    """Pre-defined system prompt configuration for the AI Assistant.

    Admins can create multiple prompts; exactly one may be active at a time.
    The custom_instructions field is injected as an additive section into
    the system prompt — the core prompt structure (persona, scope boundary,
    tool instructions, UI capabilities) remains immutable.
    """

    custom_instructions = models.TextField(
        blank=True,
        help_text=_(
            "Additional instructions injected into the system prompt. "
            "Use this for organisation-specific context, terminology, "
            "FAQ content, or behavioural guidelines. "
            "Supports {assistant_name} and {organization} placeholders."
        ),
    )
    is_active = models.BooleanField(
        default=False,
        help_text=_(
            "Whether this prompt is currently used by the AI Assistant. "
            "Only one prompt can be active."
        ),
    )

    class Meta:
        verbose_name = _("System Prompt")
        verbose_name_plural = _("System Prompts")
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="unique_active_system_prompt",
            )
        ]

    def __str__(self):
        active = " (active)" if self.is_active else ""
        return f"{self.name}{active}"

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first()


class GlobalAssistantBudget(TimeStampedModel):
    """Singleton site-wide budget covering both auth and anon traffic.

    The per-minute counter is spoofing-resistant: XFF rotation can bypass per-IP rows,
    but the global sum survives any IP fidelity issues. Fetch via ``GlobalAssistantBudget.get()``.
    """

    pk_singleton = models.PositiveSmallIntegerField(primary_key=True, default=1)

    daily_token_usage = models.PositiveBigIntegerField(default=0)
    daily_reset_last_at = models.DateTimeField(default=timezone.now)

    minute_request_usage = models.PositiveBigIntegerField(default=0)
    minute_reset_last_at = models.DateTimeField(default=timezone.now)

    PERIOD_MAP = {
        "daily": ("daily_token_usage", "AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET"),
    }

    class Meta:
        verbose_name = _("Global Assistant Budget")
        verbose_name_plural = _("Global Assistant Budgets")

    def __str__(self):
        return "GlobalAssistantBudget(singleton)"

    @classmethod
    def get(cls, lock: bool = False) -> "GlobalAssistantBudget":
        if lock and not connection.in_atomic_block:
            raise RuntimeError(
                "Locking the global budget requires transaction.atomic()."
            )
        if lock:
            try:
                return cls.objects.select_for_update().get(pk=1)
            except cls.DoesNotExist:
                row, _created = cls.objects.get_or_create(pk_singleton=1)
                return cls.objects.select_for_update().get(pk=row.pk)
        else:
            row, _created = cls.objects.get_or_create(pk_singleton=1)
            return row

    def ensure_period_reset(self) -> None:
        if not connection.in_atomic_block:
            raise RuntimeError("ensure_period_reset requires transaction.atomic().")

        now = timezone.now()
        update_fields: list[str] = []

        minute_start = now.replace(second=0, microsecond=0)
        if self.minute_reset_last_at < minute_start:
            self.minute_request_usage = 0
            self.minute_reset_last_at = minute_start
            update_fields.extend(["minute_request_usage", "minute_reset_last_at"])

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.daily_reset_last_at < day_start:
            self.daily_token_usage = 0
            self.daily_reset_last_at = day_start
            update_fields.extend(["daily_token_usage", "daily_reset_last_at"])

        if update_fields:
            self.save(update_fields=update_fields)

    def add_usage(self, tokens: int = 0) -> None:
        if not connection.in_atomic_block:
            raise RuntimeError("add_usage requires transaction.atomic().")
        if tokens < 0:
            raise ValueError(f"Token count must be non-negative, got tokens={tokens}")

        # Reset before incrementing so usage that crosses a period boundary
        # lands in the new bucket rather than a stale row the next gate read
        # would zero.
        self.ensure_period_reset()

        # ``minute_request_usage`` is incremented by ``enforce_global_budget``
        # at admission time — see budget_gate.py for the rationale (the burst
        # cap has to count attempts, not completions, to actually limit a flood).
        GlobalAssistantBudget.objects.filter(pk=self.pk).update(
            daily_token_usage=models.F("daily_token_usage") + tokens,
        )
        self.refresh_from_db(fields=["daily_token_usage"])

    def get_effective_limit(self, period: str) -> int:
        if period not in self.PERIOD_MAP:
            raise ValueError(f"Invalid period: {period}")
        _, constance_attr = self.PERIOD_MAP[period]
        cap = int(getattr(config, constance_attr))
        return cap if cap > 0 else TokenLimit.UNLIMITED

    def is_period_exhausted(self, period: str) -> bool:
        cap = self.get_effective_limit(period)
        if cap == TokenLimit.UNLIMITED:
            return False
        usage_attr, _ = self.PERIOD_MAP[period]
        return getattr(self, usage_attr) >= cap


# Anonymous-marketplace-assistant models. Imported here so Django's app
# registry picks them up; the source-of-truth definitions live in
# chat/anonymous/models.py to keep this monolithic file from sprawling.
from waldur_mastermind.chat.anonymous.models import (  # noqa: E402,F401
    AnonymousChatBudget,
    AnonymousChatClick,
    AnonymousChatFeedback,
    AnonymousChatInteraction,
    SessionBinding,
)
