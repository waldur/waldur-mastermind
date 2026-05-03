"""Site-wide capacity gate shared by the auth and anon chat paths.
``CapacityException`` must be paired with ``capacity_exception_handler`` (installed via ``get_exception_handler``) to propagate the ``Retry-After`` header.
"""

from datetime import datetime, timedelta

from constance import config
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import status
from rest_framework.views import exception_handler as drf_exception_handler

from waldur_mastermind.chat.models import GlobalAssistantBudget


class CapacityException(rf_exceptions.APIException):
    """API exception with ``Retry-After`` header and structured body (``error``, ``code``, ``reset_at``, ``retry_after_seconds``)."""

    default_detail = _("Service is temporarily over capacity.")

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        reset_at: datetime,
        message: str | None = None,
    ):
        retry_after_seconds = max(int((reset_at - timezone.now()).total_seconds()), 1)
        body = {
            "error": "rate_limited"
            if status_code == status.HTTP_429_TOO_MANY_REQUESTS
            else "service_unavailable",
            "code": code,
            "reset_at": reset_at.replace(microsecond=0).isoformat(),
            "retry_after_seconds": retry_after_seconds,
            "detail": str(message or self.default_detail),
        }
        super().__init__(detail=body)
        self.status_code = status_code
        self.headers = {"Retry-After": str(retry_after_seconds)}


def capacity_exception_handler(exc, context):
    """Propagates ``CapacityException.headers`` (Retry-After) onto the DRF response."""
    response = drf_exception_handler(exc, context)
    if response is not None and hasattr(exc, "headers"):
        for header, value in exc.headers.items():
            response[header] = value
    return response


def next_minute_boundary(now: datetime | None = None) -> datetime:
    base = now or timezone.now()
    return base.replace(second=0, microsecond=0) + timedelta(minutes=1)


def next_utc_midnight(now: datetime | None = None) -> datetime:
    base = now or timezone.now()
    return base.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def global_daily_usage() -> dict[str, int]:
    """Today's site-wide token usage as written by the rate limiter (``GlobalAssistantBudget`` singleton).

    Read-only — no lock, no atomic block. Reset boundary is enforced lazily by writers.
    Returns zeros when the singleton row doesn't exist yet.
    """
    row = GlobalAssistantBudget.objects.filter(pk=1).first()
    if row is None:
        return {"tokens": 0}
    return {"tokens": int(row.daily_token_usage)}


def global_daily_limits() -> dict[str, int]:
    """Daily token cap from Constance. ``0`` (or missing) means no cap configured.

    The per-minute burst cap (``AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE``) is enforced
    separately by ``enforce_global_budget``; it is not surfaced here because
    "requests today" is not a tracked metric — we only count the current minute.
    """
    token_cap = int(getattr(config, "AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET", 0) or 0)
    return {"tokens": max(token_cap, 0)}


def enforce_global_budget() -> None:
    """Raises ``CapacityException`` (429 burst / 503 token) when any cap is exhausted. Must run inside ``transaction.atomic()``.

    On admission, increments the per-minute counter under the same lock — the
    burst cap measures *attempts* in the current minute, not completions. This
    is the property that actually defends against concurrent floods.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("enforce_global_budget must run in transaction.atomic()")
    budget = GlobalAssistantBudget.get(lock=True)
    budget.ensure_period_reset()

    minute_cap = int(config.AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE)
    if minute_cap > 0 and budget.minute_request_usage >= minute_cap:
        raise CapacityException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="global_minute_burst",
            reset_at=next_minute_boundary(),
            message=_("AI assistant is busy. Please try again in a moment."),
        )

    if budget.is_period_exhausted("daily"):
        raise CapacityException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="global_daily_token",
            reset_at=next_utc_midnight(),
            message=_("AI assistant has reached today's token budget."),
        )

    # Bump only after every cap passes — a request being rejected because
    # the burst cap is full shouldn't push the counter further past it.
    GlobalAssistantBudget.objects.filter(pk=budget.pk).update(
        minute_request_usage=models.F("minute_request_usage") + 1,
    )
