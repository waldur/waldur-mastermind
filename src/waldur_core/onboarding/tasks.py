import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from . import enums
from .models import OnboardingVerification

logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.onboarding.expire_stale_verifications")
def expire_stale_verifications():
    """
    This task runs hourly to check for verifications that have passed their
    expiration date while still in PENDING or ESCALATED status.
    """
    now = timezone.now()

    # Find all verifications that are expired and still in PENDING or ESCALATED status
    expired_verifications = OnboardingVerification.objects.filter(
        expires_at__lt=now,
        status__in=[
            enums.VerificationStatus.PENDING,
            enums.VerificationStatus.ESCALATED,
        ],
    )

    count = expired_verifications.count()

    if count == 0:
        logger.info("No expired verifications found.")
        return

    expired_verifications.update(
        status=enums.VerificationStatus.EXPIRED,
        error_message="VERIFICATION_EXPIRED",
        error_traceback="Verification expired without completion.",
    )

    logger.info(f"Successfully marked {count} verification(s) as EXPIRED.")


@shared_task(name="waldur_core.onboarding.delete_old_verifications")
def delete_old_verifications():
    """
    This task runs daily to delete verifications that are in FAILED or EXPIRED
    status and were last modified more than 30 days ago.
    """

    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)

    # Find all verifications that are old and in FAILED or EXPIRED status
    old_verifications = OnboardingVerification.objects.filter(
        modified__lt=thirty_days_ago,
        status__in=[
            enums.VerificationStatus.FAILED,
            enums.VerificationStatus.EXPIRED,
        ],
    )

    count = old_verifications.count()

    if count == 0:
        logger.info("No old verifications found to delete.")
        return

    old_verifications.delete()

    logger.info(f"Successfully deleted {count} old verification(s).")
