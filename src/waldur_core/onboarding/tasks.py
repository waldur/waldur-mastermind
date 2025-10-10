import logging

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
