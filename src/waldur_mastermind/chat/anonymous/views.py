"""ViewSets for the anonymous chat feature.

Forking ``ChatViewSet`` into a separate file is intentional —
``LLMConfigurationMixin`` does auth-only RBAC and subclassing it would
force us to fight those defaults at every step.
"""

import json
import logging
import re
import uuid as _uuid
from datetime import timedelta

from constance import config
from django.db import transaction
from django.db.models import Avg, Count, Q, Sum
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import decorators, status, viewsets
from rest_framework import exceptions as rf_exceptions
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core.exceptions import ExtensionDisabled
from waldur_core.core.utils import get_ip_address
from waldur_core.core.views import ReadOnlyActionsViewSet
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.chat.anonymous import aggregates
from waldur_mastermind.chat.anonymous import filters as anonymous_filters
from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.anonymous import serializers as anonymous_serializers
from waldur_mastermind.chat.anonymous.catalog import build_catalog_summary
from waldur_mastermind.chat.anonymous.helpers import (
    build_domain_context,
    build_offering_format_hint,
    build_session_history,
    compute_feedback_token,
    compute_user_slug,
    verify_feedback_token,
)
from waldur_mastermind.chat.anonymous.persona import ANONYMOUS_SYSTEM_PROMPT
from waldur_mastermind.chat.budget_gate import (
    CapacityException,
    capacity_exception_handler,
    enforce_global_budget,
    global_daily_limits,
    global_daily_usage,
    next_utc_midnight,
)
from waldur_mastermind.chat.input_guards import (
    DetectionAction,
    SeverityLevel,
    get_detection_service,
)
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.models import GlobalAssistantBudget, TokenQuota
from waldur_mastermind.chat.tools.marketplace.helpers import (
    is_public_marketplace_enabled,
)
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import ANONYMOUS_TOOLS

logger = logging.getLogger(__name__)


def _missing_config(message) -> rf_exceptions.APIException:
    """409 mirrors ``ChatViewSet``'s LLMConfigurationMixin convention."""
    exc = rf_exceptions.APIException(message)
    exc.status_code = status.HTTP_409_CONFLICT
    return exc


_QUOTA_EXCEEDED_MESSAGES = {
    "daily": _("Daily token limit reached. Try again tomorrow."),
    "weekly": _("Weekly token limit reached. Try again next week."),
    "monthly": _("Monthly token limit reached. Try again next month."),
}


def _check_anonymous_chat_enabled() -> None:
    if not config.AI_ASSISTANT_ENABLED:
        raise ExtensionDisabled(
            _("The anonymous marketplace assistant is not enabled.")
        )
    if config.AI_ASSISTANT_ENABLED_ROLES != "anonymous":
        raise ExtensionDisabled(
            _("The anonymous marketplace assistant is not enabled.")
        )
    if not is_public_marketplace_enabled():
        raise ExtensionDisabled(_("Public marketplace browsing is currently disabled."))
    if not config.AI_ASSISTANT_API_URL:
        raise _missing_config(
            _("The anonymous marketplace assistant API URL is not configured.")
        )
    if not config.AI_ASSISTANT_API_TOKEN:
        raise _missing_config(
            _("The anonymous marketplace assistant API token is not configured.")
        )


def _enforce_per_ip_budget(
    ip_address: str,
) -> anonymous_models.AnonymousChatBudget:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("_enforce_per_ip_budget must run in transaction.atomic()")

    budget = anonymous_models.AnonymousChatBudget.for_ip(ip_address, lock=True)
    budget.ensure_period_reset()

    if budget.is_blocked_until and budget.is_blocked_until > timezone.now():
        raise rf_exceptions.PermissionDenied(
            _(
                "Your access to the anonymous marketplace assistant is "
                "temporarily blocked."
            )
        )

    # Check daily → weekly → monthly; first exhausted window wins.
    # Mirrors the auth-path iteration order in chat/views.py.
    for period in ("daily", "weekly", "monthly"):
        if budget.is_period_exhausted(period):
            raise CapacityException(
                status_code=status.HTTP_409_CONFLICT,
                code=f"per_ip_{period}_token",
                reset_at=TokenQuota.calculate_next_reset(period),
                message=_QUOTA_EXCEEDED_MESSAGES[period],
            )

    return budget


def _enforce_session_binding(session_id: str, ip_address: str) -> None:
    """Enforcement is here rather than in ``SessionBinding.claim`` so response shape can vary per call site."""
    binding = anonymous_models.SessionBinding.claim(session_id, ip_address)
    if binding.ip_address != ip_address:
        raise rf_exceptions.PermissionDenied(
            _(
                "Session is bound to a different network. Please start a new conversation."
            )
        )


def _build_anonymous_messages(
    user_input: str, history: list[dict] | None = None
) -> list[dict]:
    """System prompt + filtered session history + new user input."""
    system_prompt = ANONYMOUS_SYSTEM_PROMPT.format(
        assistant_name=config.AI_ASSISTANT_NAME,
        organization=config.SITE_NAME,
        domain_context=build_domain_context(),
        tools=tool_registry.get_tools_prompt(ANONYMOUS_TOOLS),
        catalog=build_catalog_summary(),
        offering_format_hint=build_offering_format_hint(),
    )
    return [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_input},
    ]


def _strike_and_maybe_block(budget: anonymous_models.AnonymousChatBudget) -> None:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("_strike_and_maybe_block must run in transaction.atomic()")
    budget.increment_injection_strikes()
    if budget.daily_injection_strikes < anonymous_models.INJECTION_STRIKE_THRESHOLD:
        return
    if not anonymous_models.INJECTION_BLOCK_HOURS:
        return
    budget.is_blocked_until = timezone.now() + timedelta(
        hours=anonymous_models.INJECTION_BLOCK_HOURS
    )
    budget.save(update_fields=["is_blocked_until"])


def _format_ndjson(data: dict) -> str:
    return f"{json.dumps(data, separators=(',', ':'))}\n"


def _wrap_stream_with_anon_finalization(
    streamer: LLMStreamer,
    interaction: anonymous_models.AnonymousChatInteraction,
    feedback_token: str,
    ip_address: str,
    detection_action: DetectionAction,
    detection_warning: str,
):
    """Persistence runs in finally so a client disconnect mid-stream still commits.

    Frame ordering:
      1. ``{"m": {"interaction_uuid": ..., "feedback_token": ...}}``  ← up-front
      2. ... LLM frames yielded by the streamer ...
      3. ``{"m": {"input_tokens": ..., "output_tokens": ...}}``       ← end
    """
    yield _format_ndjson(
        {
            "m": {
                "interaction_uuid": str(interaction.uuid),
                "feedback_token": feedback_token,
            }
        }
    )
    try:
        yield from streamer
    finally:
        try:
            with transaction.atomic():
                interaction_locked = anonymous_models.AnonymousChatInteraction.objects.select_for_update().get(
                    pk=interaction.pk
                )
                interaction_locked.assistant_blocks = streamer.accumulated_blocks or []
                interaction_locked.offering_uuids = _extract_offering_uuids(
                    streamer.accumulated_blocks or []
                )
                interaction_locked.result_count = len(interaction_locked.offering_uuids)
                interaction_locked.last_active_at = timezone.now()
                # Kept per-turn: the budget counters below collapse input and
                # output into one total, so the split survives only on the row.
                interaction_locked.input_tokens = streamer.input_tokens
                interaction_locked.output_tokens = streamer.output_tokens
                if detection_action != DetectionAction.ALLOW:
                    interaction_locked.action_taken = detection_action.value
                if detection_warning:
                    interaction_locked.warning = detection_warning
                interaction_locked.save(
                    update_fields=[
                        "assistant_blocks",
                        "offering_uuids",
                        "result_count",
                        "last_active_at",
                        "input_tokens",
                        "output_tokens",
                        "action_taken",
                        "warning",
                    ]
                )

                tokens_used = (streamer.input_tokens or 0) + (
                    streamer.output_tokens or 0
                )
                ip_budget = anonymous_models.AnonymousChatBudget.for_ip(
                    ip_address, lock=True
                )
                ip_budget.ensure_period_reset()
                ip_budget.add_usage(tokens=tokens_used)

                global_budget = GlobalAssistantBudget.get(lock=True)
                global_budget.add_usage(tokens=tokens_used)
        except Exception:
            logger.exception(
                "Anonymous chat finalization failed for interaction %s",
                interaction.uuid,
            )

        yield _format_ndjson(
            {
                "m": {
                    "input_tokens": streamer.input_tokens or 0,
                    "output_tokens": streamer.output_tokens or 0,
                }
            }
        )


# Offerings surface as ``homeport_nav`` link blocks (search_offerings / get_offering
# render marketplace-public-offering URLs); the UUID lives in the link path. Matches
# the same UUID the frontend click attribution reads from the href.
_OFFERING_URL_RE = re.compile(r"/marketplace-public-offering/([0-9a-fA-F-]{32,36})/")


def _harvest_offering_uuids(block: dict, uuids: list[str]) -> None:
    """Collect offering UUIDs from one block, whatever shape it carries."""
    # Navigation link blocks — the assistant's button-styled offering links.
    for link in block.get("links") or []:
        if isinstance(link, dict):
            match = _OFFERING_URL_RE.search(link.get("url") or "")
            if match:
                uuids.append(match.group(1))
    # Prose that embeds marketplace URLs. The assistant often answers with plain
    # markdown links instead of a nav block, and the frontend reports a click on
    # any anchor alike; skipping these made those clicks fail validation.
    content = block.get("content")
    if isinstance(content, str):
        uuids.extend(_OFFERING_URL_RE.findall(content))
    # Defensive: some tools may return structured ``data`` instead.
    data = block.get("data")
    if isinstance(data, dict):
        if data.get("uuid"):
            uuids.append(str(data["uuid"]))
        for entry in data.get("offerings") or []:
            if isinstance(entry, dict) and entry.get("uuid"):
                uuids.append(str(entry["uuid"]))


def _extract_offering_uuids(blocks: list[dict]) -> list[str]:
    """Offering UUIDs the visitor was actually shown, in render order.

    This column is what the click endpoint validates against, so it has to cover
    every surface that renders an offering link — nav blocks and markdown prose
    alike. Anything rendered but not harvested makes that click unattributable.
    """
    uuids: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        _harvest_offering_uuids(block, uuids)
        # Tool blocks wrap their payload one level down.
        result = block.get("result")
        if isinstance(result, dict):
            _harvest_offering_uuids(result, uuids)
    seen: set[str] = set()
    deduped: list[str] = []
    for u in uuids:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


class MarketplaceChatViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    def get_exception_handler(self):
        # Scoped to this viewset so Retry-After from CapacityException doesn't bleed into the rest of the project.
        return capacity_exception_handler

    @extend_schema(
        request=anonymous_serializers.AnonymousChatStreamRequestSerializer,
        responses={
            (200, "application/x-ndjson"): OpenApiResponse(
                description="NDJSON stream of assistant content blocks."
            ),
            403: OpenApiResponse(
                description="Session bound to different IP, or IP blocked."
            ),
            409: OpenApiResponse(
                description=(
                    "Per-IP daily request or token cap exhausted, or the "
                    "anonymous chat API URL/token is not configured. Carries "
                    "Retry-After header + structured rate-limit body for "
                    "the per-IP variants."
                )
            ),
            400: OpenApiResponse(description="Input rejected by injection/PII guard."),
            424: OpenApiResponse(
                description=(
                    "Anonymous chat is disabled (master switch off "
                    "or public marketplace browsing disabled)."
                )
            ),
            429: OpenApiResponse(
                description=(
                    "Site-wide per-minute burst cap exhausted. Retry-After "
                    "header + structured rate-limit body."
                )
            ),
            503: OpenApiResponse(
                description=(
                    "Site-wide daily token or request budget exhausted. "
                    "Retry-After header + structured rate-limit body."
                )
            ),
        },
        description=(
            "Anonymous chat streaming endpoint. Returns NDJSON with "
            "one assistant content block per line. Final `m` frame carries "
            "input/output token counts."
        ),
    )
    @decorators.action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def stream(self, request):
        _check_anonymous_chat_enabled()

        serializer = anonymous_serializers.AnonymousChatStreamRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        user_input = serializer.validated_data["input"]
        session_id = serializer.validated_data["session_id"]

        # Trust X-Forwarded-For — the standard Waldur deployment runs
        # gunicorn behind a reverse proxy that strips inbound XFF and
        # rewrites it with the real client IP (see
        # ``docker/rootfs/etc/waldur/gunicorn.conf.py``:
        # ``forwarded_allow_ips = "*"``). If you direct-expose gunicorn,
        # XFF becomes client-controlled and per-IP rate limits can be
        # evaded — that's a deployment-level concern, not a code-level one.
        ip_address = get_ip_address(request) or ""
        if not ip_address:
            raise rf_exceptions.PermissionDenied(_("Cannot determine client IP."))

        # Budget gates are atomic so the lazy-reset writes commit together.
        # Usage is added back in the stream finalizer where the row is re-fetched with a fresh lock.
        with transaction.atomic():
            enforce_global_budget()
            _enforce_per_ip_budget(ip_address)

        _enforce_session_binding(session_id, ip_address)

        detection = get_detection_service().check_user_input(user_input)

        if detection.action == DetectionAction.BLOCK:
            with transaction.atomic():
                strike_budget = anonymous_models.AnonymousChatBudget.for_ip(
                    ip_address, lock=True
                )
                if detection.injection.severity >= SeverityLevel.HIGH:
                    _strike_and_maybe_block(strike_budget)
            raise rf_exceptions.ValidationError(
                {"detail": detection.pii.user_message or _("Input rejected.")},
                code="input_rejected",
            )

        # LLM still gets the raw text — only the persisted form is redacted.
        user_input_to_store = (
            detection.pii.redacted_text
            if detection.action == DetectionAction.REDACT
            else user_input
        )

        user_slug = compute_user_slug(ip_address)

        # Pull session history before persisting this turn so the queryset
        # naturally excludes the row we're about to create.
        history = build_session_history(session_id)

        with transaction.atomic():
            interaction = anonymous_models.AnonymousChatInteraction.objects.create(
                user_slug=user_slug,
                user_input=user_input_to_store[:2000],
                ip_address=ip_address,
                session_id=session_id,
                last_active_at=timezone.now(),
                is_flagged=detection.is_flagged,
                severity=detection.severity.value if detection.is_flagged else "",
                injection_categories=[
                    p.get("name", "") or p.get("category", "")
                    for p in detection.injection.matched_patterns or []
                ],
                pii_categories=sorted(
                    {
                        d.get("entity_type", "")
                        for d in detection.pii.pii_detections
                        if d.get("entity_type")
                    }
                ),
                action_taken=detection.action.value,
            )

        feedback_token = compute_feedback_token(
            interaction_uuid=str(interaction.uuid),
            session_id=session_id,
            ip_address=ip_address,
        )

        messages = _build_anonymous_messages(user_input, history=history)

        streamer = LLMStreamer(
            messages=messages,
            url=config.AI_ASSISTANT_API_URL,
            token=config.AI_ASSISTANT_API_TOKEN,
            user=None,
            thread=None,
            original_input=user_input_to_store,
            pii_warning=(
                detection.pii.user_message
                if detection.action in (DetectionAction.REDACT, DetectionAction.WARN)
                else None
            ),
            worker_timeout=config.AI_ASSISTANT_STREAM_TIMEOUT_SECONDS,
            # Anon path = catalog browsing only. Pinning required halves
            # measured hallucination rate vs auto on qwen-class models;
            # tool-use-trained models (GPT-4, Claude) handle it gracefully
            # too. See validation-experiments.md for the A/B evidence.
            tool_choice_override="required",
        )

        return StreamingHttpResponse(
            _wrap_stream_with_anon_finalization(
                streamer,
                interaction,
                feedback_token,
                ip_address,
                detection.action,
                detection.pii.user_message
                if detection.action == DetectionAction.REDACT
                else "",
            ),
            content_type="application/x-ndjson",
        )

    @extend_schema(
        request=anonymous_serializers.AnonymousChatFeedbackRequestSerializer,
        responses={
            200: OpenApiResponse(description="Feedback recorded."),
            400: OpenApiResponse(
                description="Invalid body shape, or comment was rejected by injection guard."
            ),
            403: OpenApiResponse(description="Missing or forged feedback_token."),
            404: OpenApiResponse(description="Unknown interaction_uuid."),
        },
    )
    @decorators.action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def feedback(self, request):
        _check_anonymous_chat_enabled()

        serializer = anonymous_serializers.AnonymousChatFeedbackRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        interaction = self._lookup_interaction(data["interaction_uuid"])
        self._verify_feedback_token(interaction, data["feedback_token"], request)

        comment = (data.get("comment") or "").strip()
        if comment:
            detection = get_detection_service().check_user_input(comment)
            if detection.action == DetectionAction.BLOCK:
                raise rf_exceptions.ValidationError(
                    {"detail": _("Comment was rejected.")},
                    code="comment_rejected",
                )

        with transaction.atomic():
            anonymous_models.AnonymousChatFeedback.objects.update_or_create(
                interaction=interaction,
                defaults={
                    "score": data["score"],
                    "comment": comment,
                    "category": data.get("category") or "",
                    "submitted_from_ip": get_ip_address(request) or None,
                    "submitted_at": timezone.now(),
                },
            )

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        request=anonymous_serializers.AnonymousChatClickRequestSerializer,
        responses={
            200: OpenApiResponse(description="Click recorded."),
            400: OpenApiResponse(
                description="offering_uuid not in interaction's recommended set."
            ),
            403: OpenApiResponse(description="Missing or forged feedback_token."),
            404: OpenApiResponse(description="Unknown interaction_uuid."),
        },
    )
    @decorators.action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def click(self, request):
        _check_anonymous_chat_enabled()

        serializer = anonymous_serializers.AnonymousChatClickRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        interaction = self._lookup_interaction(data["interaction_uuid"])
        self._verify_feedback_token(interaction, data["feedback_token"], request)

        # Normalize via uuid.UUID — interaction.offering_uuids is stored
        # via JSONField + Waldur's StringUUID (no dashes), while the input
        # is a DRF UUIDField giving a stdlib uuid.UUID (with dashes). A
        # naive str() comparison never matches.
        def _norm(value) -> str:
            try:
                return str(_uuid.UUID(str(value)))
            except (ValueError, AttributeError):
                return str(value)

        recommended = {_norm(u) for u in (interaction.offering_uuids or [])}
        if _norm(data["offering_uuid"]) not in recommended:
            raise rf_exceptions.ValidationError(
                {"offering_uuid": _("Offering was not recommended in this session.")}
            )

        anonymous_models.AnonymousChatClick.objects.create(
            interaction=interaction,
            offering_uuid=data["offering_uuid"],
        )
        return Response(status=status.HTTP_200_OK)

    @staticmethod
    def _lookup_interaction(
        interaction_uuid,
    ) -> anonymous_models.AnonymousChatInteraction:
        try:
            return anonymous_models.AnonymousChatInteraction.objects.get(
                uuid=interaction_uuid
            )
        except anonymous_models.AnonymousChatInteraction.DoesNotExist:
            raise rf_exceptions.NotFound(_("Interaction not found."))

    @staticmethod
    def _verify_feedback_token(interaction, supplied_token: str, request) -> None:
        """ip/session come from the persisted interaction, not the current request — XFF spoofing can't forge a token."""
        if not verify_feedback_token(
            supplied_token,
            interaction_uuid=str(interaction.uuid),
            session_id=interaction.session_id,
            ip_address=interaction.ip_address,
        ):
            raise rf_exceptions.PermissionDenied(_("Invalid feedback token."))


class AnonymousChatInteractionViewSet(ReadOnlyActionsViewSet):
    """Staff/support only. ``get_queryset`` returns ``.none()`` for anyone outside the role gate
    so a future action missing ``_permissions`` can't leak transcripts.
    """

    queryset = anonymous_models.AnonymousChatInteraction.objects.all()
    serializer_class = anonymous_serializers.AnonymousChatInteractionSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "options"]
    permission_classes = (IsAuthenticated, core_permissions.ActionsPermission)
    filterset_class = anonymous_filters.AnonymousChatInteractionFilter

    list_permissions = [structure_permissions.is_staff_or_support]
    retrieve_permissions = [structure_permissions.is_staff_or_support]
    by_user_detail_permissions = [structure_permissions.is_staff_or_support]
    by_user_list_permissions = [structure_permissions.is_staff_or_support]
    by_session_permissions = [structure_permissions.is_staff_or_support]
    conversations_permissions = [structure_permissions.is_staff_or_support]
    kpi_permissions = [structure_permissions.is_staff_or_support]
    budget_snapshot_permissions = [structure_permissions.is_staff_or_support]

    def get_queryset(self):
        # Defense-in-depth — a future action missing ``_permissions`` returns
        # nothing rather than leaking.
        user = self.request.user
        if not (user.is_staff or user.is_support):
            return anonymous_models.AnonymousChatInteraction.objects.none()
        return (
            anonymous_models.AnonymousChatInteraction.objects.select_related("feedback")
            .all()
            .order_by("-created")
        )

    @extend_schema(
        summary="Full transcript for one anonymous session",
        description=(
            "Returns the ordered list of interactions belonging to the "
            "given ``session_id``. Use this to read a conversation as a "
            "transcript."
        ),
        responses={
            200: anonymous_serializers.AnonymousChatInteractionSerializer(many=True)
        },
    )
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path=r"by-session/(?P<session_id>[^/.]+)",
    )
    def by_session(self, request, session_id=None):
        # Annotated here rather than on get_queryset(): ``conversations`` runs
        # .values().annotate() over the same base queryset, and a pre-existing
        # annotation would join into that GROUP BY and skew the aggregates.
        rows = (
            self.get_queryset()
            .filter(session_id=session_id)
            .annotate(click_count=Count("clicks"))
            .order_by("created")
        )
        return Response(self.get_serializer(rows, many=True).data)

    @extend_schema(
        operation_id="anonymous_chat_interactions_by_user_aggregate",
        summary="Aggregate user list (no slug)",
        description=(
            "Returns one row per user_slug with aggregate counters. "
            "Powers the staff Users page in the admin analytics."
        ),
        responses={
            200: anonymous_serializers.AnonymousChatUserAggregateSerializer(many=True)
        },
    )
    @decorators.action(detail=False, methods=["get"], url_path="by-user")
    def by_user_list(self, request):
        qs = self.filter_queryset(self.get_queryset())
        rows = aggregates.user_aggregates(qs)
        return Response(
            anonymous_serializers.AnonymousChatUserAggregateSerializer(
                rows, many=True
            ).data
        )

    @extend_schema(
        summary="All sessions for one pseudonymous user",
        description=(
            "Returns interactions sharing a ``user_slug`` (Scrypt of "
            "originating IP) — across however many sessions that anon "
            "user opened, ordered chronologically."
        ),
        responses={
            200: anonymous_serializers.AnonymousChatInteractionSerializer(many=True)
        },
    )
    @decorators.action(
        detail=False,
        methods=["get"],
        url_path=r"by-user/(?P<user_slug>[^/.]+)",
        url_name="by-user",
    )
    def by_user_detail(self, request, user_slug=None):
        rows = self.get_queryset().filter(user_slug=user_slug).order_by("created")
        return Response(self.get_serializer(rows, many=True).data)

    @extend_schema(
        summary="Conversations grouped by session",
        description=(
            "One row per anonymous conversation (``session_id``) with "
            "aggregate counters, mirroring the authenticated threads table. "
            "Honours the same filters as the list endpoint. Read a "
            "conversation's transcript via ``by-session/{session_id}``."
        ),
        responses={
            200: anonymous_serializers.AnonymousChatConversationSerializer(many=True)
        },
    )
    @decorators.action(detail=False, methods=["get"], url_path="conversations")
    def conversations(self, request):
        qs = self.filter_queryset(self.get_queryset())
        rows = aggregates.session_aggregates(qs)
        return Response(
            anonymous_serializers.AnonymousChatConversationSerializer(
                rows, many=True
            ).data
        )

    @extend_schema(
        summary="Aggregate KPI roll-up",
        description=(
            "Returns aggregate counters and rates for the anonymous "
            "chat flow. Filters are honoured (date range etc.) so "
            "the same parameters work as on the list endpoint."
        ),
        responses={200: anonymous_serializers.AnonymousChatKpiResponseSerializer},
    )
    @decorators.action(detail=False, methods=["get"])
    def kpi(self, request):
        qs = self.filter_queryset(self.get_queryset())

        agg = qs.aggregate(
            interactions_total=Count("uuid"),
            sessions_total=Count("session_id", distinct=True),
            unique_users=Count(
                "user_slug",
                distinct=True,
                filter=~Q(user_slug=""),
            ),
            flagged_total=Count("uuid", filter=Q(is_flagged=True)),
            feedback_positive=Count("uuid", filter=Q(feedback__score=1)),
            feedback_negative=Count("uuid", filter=Q(feedback__score=-1)),
            # Safe to sum in the same call only because the sole join here is
            # the 1:1 feedback row. The many-side clicks stay in their own
            # query below precisely to keep this GROUP BY from multiplying.
            input_tokens_total=Sum("input_tokens"),
            output_tokens_total=Sum("output_tokens"),
        )

        positive = agg["feedback_positive"] or 0
        negative = agg["feedback_negative"] or 0
        total_human = positive + negative
        satisfaction_rate = (positive / total_human) if total_human else None

        clicks_total = anonymous_models.AnonymousChatClick.objects.filter(
            interaction__in=qs
        ).count()
        interactions_total = agg["interactions_total"] or 0
        click_through_rate = (
            (clicks_total / interactions_total) if interactions_total else None
        )

        payload = {
            "interactions_total": interactions_total,
            "sessions_total": agg["sessions_total"] or 0,
            "unique_users": agg["unique_users"] or 0,
            "flagged_total": agg["flagged_total"] or 0,
            "feedback_positive": positive,
            "feedback_negative": negative,
            "satisfaction_rate": satisfaction_rate,
            "clicks_total": clicks_total,
            "click_through_rate": click_through_rate,
            # Null on an empty set, and on turns predating token capture.
            "input_tokens_total": agg["input_tokens_total"] or 0,
            "output_tokens_total": agg["output_tokens_total"] or 0,
        }

        reviewed_qs = anonymous_models.AnonymousChatFeedback.objects.filter(
            interaction__in=qs,
            llm_reviewed_at__isnull=False,
        )
        review_agg = reviewed_qs.aggregate(
            reviewed_total=Count("interaction"),
            avg_resolution=Avg("llm_resolution_score"),
            hallucinations=Count(
                "interaction", filter=Q(llm_hallucination_detected=True)
            ),
            # Kept out of the visitor totals above on purpose: the judge draws
            # on its own budget so review can't starve user-facing traffic, and
            # one merged number would hide exactly that split.
            judge_input_tokens=Sum("llm_judge_input_tokens"),
            judge_output_tokens=Sum("llm_judge_output_tokens"),
        )
        reviewed_total = review_agg["reviewed_total"] or 0
        # Unlike the rates below, these three stay in the payload at zero — a
        # review row that disappears when nothing has been judged is how a dead
        # nightly pass goes unnoticed.
        payload["reviewed_total"] = reviewed_total
        payload["review_input_tokens_total"] = review_agg["judge_input_tokens"] or 0
        payload["review_output_tokens_total"] = review_agg["judge_output_tokens"] or 0
        if reviewed_total:
            intent_distribution = dict(
                reviewed_qs.exclude(llm_intent_category="")
                .values_list("llm_intent_category")
                .annotate(c=Count("interaction"))
                .values_list("llm_intent_category", "c")
            )
            payload["avg_llm_resolution_score"] = review_agg["avg_resolution"]
            payload["llm_intent_distribution"] = intent_distribution
            payload["hallucination_rate"] = (
                review_agg["hallucinations"] or 0
            ) / reviewed_total
            sessions_total = agg["sessions_total"] or 0
            payload["review_coverage"] = (
                (reviewed_total / sessions_total) if sessions_total else None
            )

        try:
            window_days = max(
                1, min(365, int(request.query_params.get("window_days", 30)))
            )
        except (ValueError, TypeError):
            window_days = 30
        payload["daily_volume"] = aggregates.daily_volume(qs, days=window_days)
        payload["severity_by_day"] = aggregates.severity_by_day(qs, days=14)

        serializer = anonymous_serializers.AnonymousChatKpiResponseSerializer(payload)
        return Response(serializer.data)

    @extend_schema(
        summary="Today's global tenant budget snapshot",
        description=(
            "Returns the site-wide token + request usage accumulated since "
            "00:00 UTC today and the configured daily caps. Powers the "
            "budget gauges card on the staff analytics dashboard."
        ),
        responses={200: anonymous_serializers.AnonymousChatBudgetSnapshotSerializer},
    )
    @decorators.action(detail=False, methods=["get"], url_path="budget")
    def budget_snapshot(self, request):
        usage = global_daily_usage()
        limits = global_daily_limits()
        payload = {
            "tokens_today": usage.get("tokens", 0),
            "tokens_limit": limits.get("tokens", 0),
            "resets_at": next_utc_midnight(),
        }
        return Response(
            anonymous_serializers.AnonymousChatBudgetSnapshotSerializer(payload).data
        )


class AnonymousChatFeedbackViewSet(ReadOnlyActionsViewSet):
    """Separate ViewSet so admin UI can filter feedback independently of interaction transcripts.

    Lookup is on ``interaction__uuid`` — the feedback table has no UUID of its own.
    """

    queryset = anonymous_models.AnonymousChatFeedback.objects.all()
    serializer_class = anonymous_serializers.AnonymousChatFeedbackSerializer
    lookup_field = "interaction__uuid"
    lookup_url_kwarg = "interaction_uuid"
    http_method_names = ["get", "options"]
    permission_classes = (IsAuthenticated, core_permissions.ActionsPermission)
    filterset_class = anonymous_filters.AnonymousChatFeedbackFilter

    list_permissions = [structure_permissions.is_staff_or_support]
    retrieve_permissions = [structure_permissions.is_staff_or_support]

    def get_queryset(self):
        user = self.request.user
        if not (user.is_staff or user.is_support):
            return anonymous_models.AnonymousChatFeedback.objects.none()
        return (
            anonymous_models.AnonymousChatFeedback.objects.select_related("interaction")
            .all()
            .order_by("-submitted_at")
        )
