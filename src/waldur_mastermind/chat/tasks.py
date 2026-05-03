import logging
from datetime import timedelta

import openai
from celery import shared_task
from constance import config
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.utils import timezone

from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.anonymous.judge import (
    build_judge_messages,
    build_transcript,
    call_judge_llm,
    collect_tool_results_from_blocks,
    parse_judge_json,
)
from waldur_mastermind.chat.models import (
    AnonymousChatFeedback,
    AnonymousChatInteraction,
    ChatSession,
    TokenQuota,
)

logger = logging.getLogger(__name__)


def _reset_period(period: str):
    period_start = TokenQuota.calculate_reset_period_start(period)

    usage_field = f"{period}_usage"
    reset_field = f"{period}_reset_last_at"

    updated = TokenQuota.objects.filter(**{f"{reset_field}__lt": period_start}).update(
        **{usage_field: 0, reset_field: timezone.now()}
    )

    logger.info(f"Successfully reset {period} token usage for {updated} quotas")
    return updated


@shared_task(name="waldur_mastermind.chat.reset_daily_token_usage")
def reset_daily_token_usage():
    """Reset quotas where last reset was on a previous calendar day."""
    return _reset_period("daily")


@shared_task(name="waldur_mastermind.chat.reset_weekly_token_usage")
def reset_weekly_token_usage():
    """Reset quotas where last reset was in a previous calendar week (Monday start)."""
    return _reset_period("weekly")


@shared_task(name="waldur_mastermind.chat.reset_monthly_token_usage")
def reset_monthly_token_usage():
    """Reset quotas where last reset was in a previous calendar month."""
    return _reset_period("monthly")


@shared_task(name="waldur_mastermind.chat.cleanup_old_chat_sessions")
def cleanup_old_chat_sessions():
    """
    Delete chat sessions older than the configured retention period.

    Deletes ChatSession objects where the modified timestamp is older than
    AI_ASSISTANT_SESSION_RETENTION_DAYS. Cascading delete will automatically remove
    related ThreadSession and Message objects.

    Returns a dict with status and deleted_count.
    """
    retention_days = config.AI_ASSISTANT_SESSION_RETENTION_DAYS

    if retention_days <= 0:
        logger.info(
            "Chat session cleanup skipped: retention period is %d days",
            retention_days,
        )
        return {"status": "disabled", "deleted_count": 0}

    cutoff_date = timezone.now() - timedelta(days=retention_days)

    logger.info(
        "Starting chat session cleanup (retention: %d days, cutoff: %s)",
        retention_days,
        cutoff_date.date(),
    )

    # Filter sessions older than retention period
    old_sessions = ChatSession.objects.filter(modified__lt=cutoff_date)

    # Count before deletion for logging
    count = old_sessions.count()

    if count == 0:
        logger.info("No chat sessions older than %d days found", retention_days)
        return {"status": "success", "deleted_count": 0}

    # Delete old sessions (cascade will delete threads and messages)
    _, deletion_info = old_sessions.delete()

    logger.info(
        "Successfully deleted %d chat sessions older than %d days. "
        "Also deleted: %d threads, %d messages",
        count,
        retention_days,
        deletion_info.get("chat.ThreadSession", 0),
        deletion_info.get("chat.Message", 0),
    )

    return {
        "status": "success",
        "deleted_count": count,
        "related_objects": deletion_info,
    }


@shared_task(name="waldur_mastermind.chat.cleanup_anonymous_chat_artifacts")
def cleanup_anonymous_chat_artifacts():
    """Purge stale per-IP / per-session bookkeeping rows.

    The public anonymous endpoint creates one ``SessionBinding`` per
    distinct ``session_id`` and one ``AnonymousChatBudget`` per distinct
    REMOTE_ADDR. Without this cleanup an attacker can grow these tables
    without bound by rotating session-ids or coming from many client IPs.

    Retention is governed by ``ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS``;
    set to a negative value to disable.

    Stale ``AnonymousChatBudget`` rows whose ``is_blocked_until`` is still
    in the future are KEPT — otherwise an attacker could simply wait out
    the cleanup window to clear an active block.

    Audit value (``AnonymousChatInteraction``, ``AnonymousChatFeedback``,
    ``AnonymousChatClick``) is intentionally NOT touched here — those
    rows have separate retention managed via
    ``AI_ASSISTANT_SESSION_RETENTION_DAYS``.
    """
    retention_days = config.ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS

    if retention_days < 0:
        logger.info(
            "Anonymous chat artifact cleanup skipped: retention is %d days",
            retention_days,
        )
        return {
            "status": "disabled",
            "deleted_session_bindings": 0,
            "deleted_budgets": 0,
        }

    cutoff = timezone.now() - timedelta(days=retention_days)
    now = timezone.now()

    deleted_bindings, _ = anonymous_models.SessionBinding.objects.filter(
        last_seen__lt=cutoff
    ).delete()

    # Active blocks must survive — a stale row whose ``is_blocked_until`` is
    # still in the future is the *whole point* of the strike-then-block
    # subsystem. Idle stale rows are eligible.
    deleted_budgets, _ = (
        anonymous_models.AnonymousChatBudget.objects.filter(modified__lt=cutoff)
        .filter(Q(is_blocked_until__isnull=True) | Q(is_blocked_until__lt=now))
        .delete()
    )

    logger.info(
        "Anonymous chat artifact cleanup deleted bindings=%d budgets=%d "
        "(retention=%d days, cutoff=%s)",
        deleted_bindings,
        deleted_budgets,
        retention_days,
        cutoff.date(),
    )
    return {
        "status": "success",
        "deleted_session_bindings": deleted_bindings,
        "deleted_budgets": deleted_budgets,
    }


# Hardcoded review pacing — admins shouldn't need to tune these. The
# after-hours window prevents judging conversations still in progress;
# the batch size caps work per beat run.
_REVIEW_AFTER_HOURS = 6
_REVIEW_BATCH_SIZE = 200


@shared_task(name="waldur_mastermind.chat.review_completed_sessions")
def review_completed_sessions():
    """Nightly LLM-as-judge pass over completed anonymous chat sessions.

    Picks sessions whose **last** interaction is older than
    ``_REVIEW_AFTER_HOURS`` and that haven't been judged yet, then calls
    the same LLM as the user-facing flow with a structured judge prompt.
    The verdict (resolution score, intent category, hallucination flag,
    summary) lands on the last interaction's ``AnonymousChatFeedback``
    row via ``update_or_create`` — preserving any human thumbs that
    landed earlier.

    Bounded by:
      * ``ANONYMOUS_CHAT_REVIEW_ENABLED`` master switch (no-op when off)
      * ``_REVIEW_BATCH_SIZE`` upper bound on sessions/run
      * ``ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET`` running token cap
        (running counter, not pre-allocated — stops mid-batch when out)

    Per-session try/except ensures one bad session doesn't kill the run.
    JSON-parse failures skip the row (can be retried next pass) so a
    transient model glitch doesn't corrupt the feedback table.
    """
    if not config.ANONYMOUS_CHAT_REVIEW_ENABLED:
        logger.info(
            "Anonymous chat review skipped: ANONYMOUS_CHAT_REVIEW_ENABLED=False"
        )
        return {"status": "disabled", "reviewed": 0}

    review_budget = config.ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET
    cutoff = timezone.now() - timedelta(hours=_REVIEW_AFTER_HOURS)
    batch_size = _REVIEW_BATCH_SIZE

    # The "last interaction" filter is the load-bearing one — without it
    # any session with one early interaction would be reviewed forever
    # just because it has an old `created`. Group by session_id and
    # filter on the per-group max(last_active_at).
    # Exclude sessions that are already judged on their last interaction so
    # the LIMIT is applied only to sessions that still need judging. Order by
    # oldest `max_last_active` to drain backlog deterministically.
    already_judged_qs = AnonymousChatFeedback.objects.filter(
        interaction__session_id=OuterRef("session_id"),
        llm_reviewed_at__isnull=False,
    )

    session_max = (
        AnonymousChatInteraction.objects.exclude(session_id="")
        .values("session_id")
        .annotate(max_last_active=Max("last_active_at"))
        .annotate(has_judged=Exists(already_judged_qs))
        .filter(max_last_active__lt=cutoff, has_judged=False)
        .order_by("max_last_active")
    )

    candidate_session_ids = list(
        session_max.values_list("session_id", flat=True)[:batch_size]
    )

    reviewed = 0
    skipped_already_judged = 0
    skipped_parse_error = 0
    skipped_llm_error = 0

    for session_id in candidate_session_ids:
        if review_budget <= 0:
            logger.info(
                "Anonymous chat review budget exhausted after %d session(s); "
                "remaining batch resumes next run.",
                reviewed,
            )
            break

        interactions = list(
            AnonymousChatInteraction.objects.filter(session_id=session_id).order_by(
                "created"
            )
        )
        if not interactions:
            continue

        last_interaction = interactions[-1]

        # Skip sessions already judged on the last interaction's
        # feedback row. Cheap pre-check before we burn LLM tokens.
        already_judged = AnonymousChatFeedback.objects.filter(
            interaction=last_interaction,
            llm_reviewed_at__isnull=False,
        ).exists()
        if already_judged:
            skipped_already_judged += 1
            continue

        transcript = build_transcript(interactions)
        tool_results = collect_tool_results_from_blocks(interactions)
        messages = build_judge_messages(transcript, tool_results)

        try:
            response = call_judge_llm(messages)
        except openai.APIError:
            logger.exception(
                "Anonymous chat judge LLM call failed for session %s",
                session_id,
            )
            skipped_llm_error += 1
            continue

        verdict = parse_judge_json(response.content)
        if verdict is None:
            logger.warning(
                "Anonymous chat judge returned unparseable verdict for session %s",
                session_id,
            )
            skipped_parse_error += 1
            # Charge tokens against the budget anyway — the cost was
            # incurred regardless of parse outcome.
            review_budget -= response.input_tokens + response.output_tokens
            continue

        with transaction.atomic():
            AnonymousChatFeedback.objects.update_or_create(
                interaction=last_interaction,
                defaults={
                    "llm_resolution_score": verdict.resolution_score,
                    "llm_intent_category": verdict.intent_category,
                    "llm_hallucination_detected": verdict.hallucination_detected,
                    "llm_hallucination_details": verdict.hallucination_details,
                    "llm_summary": verdict.summary,
                    "llm_reviewed_at": timezone.now(),
                    "llm_judge_input_tokens": response.input_tokens,
                    "llm_judge_output_tokens": response.output_tokens,
                    "llm_judge_model": config.AI_ASSISTANT_MODEL,
                },
            )

        reviewed += 1
        review_budget -= response.input_tokens + response.output_tokens

    logger.info(
        "Anonymous chat review pass complete: reviewed=%d skipped_already_judged=%d "
        "skipped_parse_error=%d skipped_llm_error=%d remaining_budget=%d",
        reviewed,
        skipped_already_judged,
        skipped_parse_error,
        skipped_llm_error,
        review_budget,
    )
    return {
        "status": "ok",
        "reviewed": reviewed,
        "skipped_already_judged": skipped_already_judged,
        "skipped_parse_error": skipped_parse_error,
        "skipped_llm_error": skipped_llm_error,
        "remaining_budget": review_budget,
    }
