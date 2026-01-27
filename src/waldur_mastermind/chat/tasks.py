import logging

from celery import shared_task
from django.utils import timezone

from .models import TokenQuota

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
