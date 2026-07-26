"""Models for the anonymous chat flow. No ``User`` FK on any — identity is IP + client-generated ``session_id``."""

import logging
import uuid

from constance import config
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import connection, models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.fields import AutoCreatedField, AutoLastModifiedField
from model_utils.models import TimeStampedModel

from waldur_mastermind.chat.block_schemas import blocks_to_text
from waldur_mastermind.chat.enums import FeedbackCategory

# Imported here so AnonymousChatBudget can use the same sentinel without
# creating a new constant. Import direction is anonymous → chat (not the
# reverse) — chat/models.py re-exports from this module but does NOT import
# from it at module level beyond the re-export block at the bottom.
from waldur_mastermind.chat.models import TokenLimit, TokenQuota  # noqa: E402

logger = logging.getLogger(__name__)

# Injection-strike policy. HIGH/CRITICAL prompt-injection detections per actor
# per day before the per-actor daily token cap is cut to 10% of its default.
# Crossing the threshold also schedules an auto-block for ``INJECTION_BLOCK_HOURS``;
# set ``INJECTION_BLOCK_HOURS = 0`` to throttle without hard-blocking.
INJECTION_STRIKE_THRESHOLD = 5
INJECTION_BLOCK_HOURS = 24


class AnonymousChatBudget(TimeStampedModel):
    """Per-IP budget across daily / weekly / monthly windows.

    Use ``AnonymousChatBudget.for_ip(ip, lock=True)`` inside ``transaction.atomic()``.

    ``PERIOD_MAP`` shape is intentionally different from ``TokenQuota.PERIOD_MAP``:
    there are no per-row override columns here (anon rows are pseudonymous and
    not admin-editable), so the tuple is ``(usage_field, reset_field, constance_key)``
    rather than ``(usage, user_limit, constance)``.
    """

    ip_address = models.GenericIPAddressField(unique=True)

    daily_token_usage = models.PositiveIntegerField(default=0)
    weekly_token_usage = models.PositiveIntegerField(default=0)
    monthly_token_usage = models.PositiveIntegerField(default=0)

    daily_injection_strikes = models.PositiveIntegerField(default=0)

    daily_reset_last_at = models.DateTimeField(default=timezone.now)
    weekly_reset_last_at = models.DateTimeField(default=timezone.now)
    monthly_reset_last_at = models.DateTimeField(default=timezone.now)

    # Set when ``daily_injection_strikes`` crosses ``INJECTION_STRIKE_THRESHOLD``
    # and ``INJECTION_BLOCK_HOURS`` is non-zero (see chat/anonymous/views.py).
    is_blocked_until = models.DateTimeField(null=True, blank=True)

    PERIOD_MAP = {
        "daily": (
            "daily_token_usage",
            "daily_reset_last_at",
            "AI_ASSISTANT_TOKEN_LIMIT_DAILY",
        ),
        "weekly": (
            "weekly_token_usage",
            "weekly_reset_last_at",
            "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY",
        ),
        "monthly": (
            "monthly_token_usage",
            "monthly_reset_last_at",
            "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY",
        ),
    }

    class Meta:
        verbose_name = _("Anonymous Chat Budget")
        verbose_name_plural = _("Anonymous Chat Budgets")
        indexes = [
            models.Index(fields=["ip_address"]),
            models.Index(fields=["daily_reset_last_at"]),
            models.Index(fields=["weekly_reset_last_at"]),
            models.Index(fields=["monthly_reset_last_at"]),
        ]

    def __str__(self):
        return f"AnonymousChatBudget({self.ip_address})"

    @classmethod
    def for_ip(cls, ip_address: str, lock: bool = False) -> "AnonymousChatBudget":
        """Mirrors ``TokenQuota.for_user`` — same lock-then-create race handling."""
        if lock and not connection.in_atomic_block:
            raise RuntimeError(
                "Locking a AnonymousChatBudget requires an active transaction.atomic() block."
            )

        if lock:
            try:
                return cls.objects.select_for_update().get(ip_address=ip_address)
            except cls.DoesNotExist:
                row, _created = cls.objects.get_or_create(ip_address=ip_address)
                return cls.objects.select_for_update().get(pk=row.pk)
        else:
            row, _created = cls.objects.get_or_create(ip_address=ip_address)
            return row

    def ensure_period_reset(self) -> None:
        if not connection.in_atomic_block:
            raise RuntimeError(
                "ensure_period_reset requires an active transaction.atomic() block."
            )

        update_fields = []
        now = timezone.now()

        for period, (
            usage_field,
            reset_field,
            _constance_key,
        ) in self.PERIOD_MAP.items():
            period_start = TokenQuota.calculate_reset_period_start(period)
            if getattr(self, reset_field) < period_start:
                setattr(self, usage_field, 0)
                setattr(self, reset_field, now)
                update_fields.extend([usage_field, reset_field])
                if period == "daily":
                    # Injection strikes are per-day — reset together with daily usage.
                    self.daily_injection_strikes = 0
                    update_fields.append("daily_injection_strikes")

        if update_fields:
            self.save(update_fields=update_fields)

    def add_usage(self, tokens: int = 0) -> None:
        if not connection.in_atomic_block:
            raise RuntimeError(
                "add_usage requires an active transaction.atomic() block."
            )
        if tokens < 0:
            raise ValueError(f"Token count must be non-negative, got tokens={tokens}")

        # Update all three windows in one UPDATE — do NOT include
        # daily_injection_strikes here; that has its own write path via
        # increment_injection_strikes().
        AnonymousChatBudget.objects.filter(pk=self.pk).update(
            daily_token_usage=models.F("daily_token_usage") + tokens,
            weekly_token_usage=models.F("weekly_token_usage") + tokens,
            monthly_token_usage=models.F("monthly_token_usage") + tokens,
        )
        self.refresh_from_db(
            fields=["daily_token_usage", "weekly_token_usage", "monthly_token_usage"]
        )

    def increment_injection_strikes(self) -> None:
        """Caller sets ``is_blocked_until`` when the threshold is crossed — that logic depends on Constance values read in the view."""
        if not connection.in_atomic_block:
            raise RuntimeError(
                "increment_injection_strikes requires transaction.atomic()."
            )
        AnonymousChatBudget.objects.filter(pk=self.pk).update(
            daily_injection_strikes=models.F("daily_injection_strikes") + 1,
        )
        self.refresh_from_db(fields=["daily_injection_strikes"])

    def get_effective_limit(self, period: str) -> int:
        """Per-IP cap for the given period. Returns ``TokenLimit.UNLIMITED`` (-1) when not configured.

        For daily, applies the strike-throttle (cap // 10) once the threshold is crossed.
        Weekly / monthly use the raw cap — strikes reset daily, so carrying the reduction
        into a multi-day window would penalise future days with no signal.
        """
        if period not in self.PERIOD_MAP:
            raise ValueError(f"Invalid period: {period}")
        _, _, constance_key = self.PERIOD_MAP[period]
        raw = int(getattr(config, constance_key))
        if raw == TokenLimit.UNLIMITED:
            return TokenLimit.UNLIMITED
        cap = raw
        if (
            period == "daily"
            and self.daily_injection_strikes >= INJECTION_STRIKE_THRESHOLD
        ):
            cap = raw // 10
        return cap

    def is_period_exhausted(self, period: str) -> bool:
        """Return True when the period's usage has reached (or exceeded) the effective cap."""
        cap = self.get_effective_limit(period)
        if cap == TokenLimit.UNLIMITED:
            return False
        usage_field, _, _ = self.PERIOD_MAP[period]
        return getattr(self, usage_field) >= cap


class AnonymousChatInteraction(models.Model):
    """Anon-path twin of ``chat.Message``. ``assistant_blocks`` mirrors ``Message.blocks`` shape
    so the anonymous view can assemble LLM context from the DB on subsequent turns.
    """

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    offering_uuids = models.JSONField(default=list)
    result_count = models.PositiveIntegerField(default=0)

    # Per-turn usage, mirroring Message.input_tokens/output_tokens on the
    # authenticated side. The budget counters these also feed are collapsed to a
    # single total for rate limiting, so the split is only recoverable here.
    # Null on rows written before this was tracked.
    input_tokens = models.PositiveIntegerField(null=True, blank=True, default=None)
    output_tokens = models.PositiveIntegerField(null=True, blank=True, default=None)

    # Scrypt(salt, ip) — irreversible pseudonym so admin UI shows a stable per-actor key without exposing raw IP.
    user_slug = models.CharField(max_length=128, db_index=True, blank=True, default="")

    # When PIIDetector returns REDACT, the redacted form is stored (not raw). LLM still receives raw at request time.
    user_input = models.TextField(blank=True, default="")  # truncated 2000 chars
    assistant_blocks = models.JSONField(default=list)

    # Denormalized plain-text of the whole turn (user_input + assistant reply),
    # kept in sync in save(). Feeds `search_vector` so staff can full-text
    # search anon transcript content. blocks_to_text is the shared extractor
    # (single source of truth, also used by chat.Message and the LLM context).
    search_text = models.TextField(blank=True, default="")
    # tsvector of search_text, computed by Postgres as a stored generated
    # column (not a trigger) so it exists even when the schema is built from
    # models directly — e.g. CI's `pytest --no-migrations`.
    search_vector = models.GeneratedField(
        expression=SearchVector("search_text", config="english"),
        output_field=SearchVectorField(),
        db_persist=True,
    )

    is_flagged = models.BooleanField(default=False)
    severity = models.CharField(max_length=16, blank=True, default="")
    injection_categories = models.JSONField(default=list)
    pii_categories = models.JSONField(default=list)
    action_taken = models.CharField(max_length=16, blank=True, default="")
    warning = models.TextField(blank=True, default="")

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    session_id = models.CharField(max_length=64, blank=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    created = AutoCreatedField()

    class Meta:
        verbose_name = _("Anonymous Chat Interaction")
        verbose_name_plural = _("Anonymous Chat Interactions")
        indexes = [
            models.Index(fields=["created"]),
            models.Index(fields=["session_id", "created"]),
            models.Index(fields=["user_slug", "created"]),
            models.Index(fields=["uuid"]),
            GinIndex(fields=["search_vector"], name="anon_chat_search_gin"),
        ]

    def __str__(self):
        return f"AnonymousChatInteraction({self.uuid})"

    def _build_search_text(self):
        # One row holds both sides of the turn — combine so staff can find a
        # session by the question OR the answer. blocks_to_text handles the
        # assistant blocks; user_input is already plain text.
        parts = [self.user_input or "", blocks_to_text(self.assistant_blocks or [])]
        return "\n".join(p for p in parts if p)

    def save(self, *args, **kwargs):
        # Recompute only when a text-bearing field is part of this write. The
        # reply lands in a second save (see anonymous/views.py stream
        # finaliser); a save touching neither side can't change search_text, so
        # skip the work — and skip touching the source fields, which could
        # otherwise force a deferred fetch.
        source_fields = {"user_input", "assistant_blocks"}
        update_fields = kwargs.get("update_fields")
        if update_fields is None or source_fields & set(update_fields):
            self.search_text = self._build_search_text()
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"search_text"}
        super().save(*args, **kwargs)


class AnonymousChatFeedback(models.Model):
    """Carries human thumbs (POST /feedback/) and/or LLM-judge review. A row exists when either signal is present.

    LLM review is per-session and written to the last interaction's row — earlier rows may have no feedback row unless human-thumbed.
    """

    interaction = models.OneToOneField(
        AnonymousChatInteraction,
        on_delete=models.CASCADE,
        related_name="feedback",
        primary_key=True,
    )

    score = models.SmallIntegerField(null=True, blank=True)  # +1 / -1 / null
    comment = models.TextField(blank=True, default="")
    category = models.CharField(
        max_length=32,
        blank=True,
        default="",
        choices=FeedbackCategory.choices,
    )
    submitted_from_ip = models.GenericIPAddressField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    llm_resolution_score = models.SmallIntegerField(null=True, blank=True)  # 1–5
    # Free-form lowercase slug. The judge derives the allowed set from
    # marketplace.Category at runtime (see judge.build_intent_rubric),
    # so the column intentionally has no choices= constraint —
    # different deployments produce different slugs (e.g. an HPC site
    # has 'gpu_compute' while a gov-cloud site has 'iam'). Pre-existing
    # rows from the WAL-9688 launch use the legacy fixed set
    # ('compute', 'storage', 'software', 'consultancy', 'unclear') and
    # stay valid.
    llm_intent_category = models.CharField(
        max_length=32,
        blank=True,
        default="",
    )
    llm_hallucination_detected = models.BooleanField(default=False)
    llm_hallucination_details = models.TextField(blank=True, default="")
    llm_summary = models.TextField(blank=True, default="")
    llm_reviewed_at = models.DateTimeField(null=True, blank=True)
    llm_judge_input_tokens = models.PositiveIntegerField(default=0)
    llm_judge_output_tokens = models.PositiveIntegerField(default=0)
    llm_judge_model = models.CharField(max_length=64, blank=True, default="")

    modified_at = AutoLastModifiedField()

    class Meta:
        verbose_name = _("Anonymous Chat Feedback")
        verbose_name_plural = _("Anonymous Chat Feedbacks")
        indexes = [
            models.Index(fields=["score"]),
            models.Index(fields=["submitted_at"]),
            models.Index(fields=["llm_resolution_score"]),
            models.Index(fields=["llm_intent_category"]),
            models.Index(fields=["llm_hallucination_detected"]),
            models.Index(fields=["llm_reviewed_at"]),
        ]

    def __str__(self):
        return f"AnonymousChatFeedback(interaction={self.interaction_id})"


class SessionBinding(models.Model):
    """Pins a ``session_id`` to its originating IP — later requests from a different IP get 403.

    ``ip_address`` is immutable once set; legitimate IP changes (mobile handoff, VPN) mean a fresh conversation.
    """

    session_id = models.CharField(max_length=64, primary_key=True)
    ip_address = models.GenericIPAddressField()
    created = AutoCreatedField()
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Session Binding")
        verbose_name_plural = _("Session Bindings")
        indexes = [
            models.Index(fields=["created"]),
            models.Index(fields=["last_seen"]),
        ]

    def __str__(self):
        return f"SessionBinding({self.session_id[:8]}…)"

    @classmethod
    def claim(cls, session_id: str, ip_address: str) -> "SessionBinding":
        """Idempotent bind-or-fetch. IP mismatch is enforced by the caller — response shape differs per call site."""
        binding, _created = cls.objects.get_or_create(
            session_id=session_id,
            defaults={"ip_address": ip_address},
        )
        if not _created:
            cls.objects.filter(pk=binding.pk).update(last_seen=timezone.now())
        return binding


class AnonymousChatClick(models.Model):
    """One click per row. FK is non-unique by design — repeat clicks on the same offering are intentional."""

    interaction = models.ForeignKey(
        AnonymousChatInteraction,
        on_delete=models.CASCADE,
        related_name="clicks",
    )
    offering_uuid = models.UUIDField()
    clicked_at = AutoCreatedField()

    class Meta:
        verbose_name = _("Anonymous Chat Click")
        verbose_name_plural = _("Anonymous Chat Clicks")
        indexes = [
            models.Index(fields=["offering_uuid", "clicked_at"]),
            models.Index(fields=["clicked_at"]),
            models.Index(fields=["interaction"]),
        ]

    def __str__(self):
        return f"AnonymousChatClick({self.offering_uuid})"
