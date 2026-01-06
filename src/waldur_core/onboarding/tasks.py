import logging
from datetime import timedelta

from celery import shared_task
from constance import config
from django.utils import timezone

from waldur_core.core import utils as core_utils

from . import enums
from .models import OnboardingJustification, OnboardingVerification

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


@shared_task(name="waldur_core.onboarding.send_justification_review_notification")
def send_justification_review_notification(justification_uuid):
    """
    Send email notification to user when their justification is approved or rejected.

    Args:
        justification_uuid: UUID of the OnboardingJustification object
    """
    try:
        justification = OnboardingJustification.objects.get(uuid=justification_uuid)
    except OnboardingJustification.DoesNotExist:
        logger.error(
            f"OnboardingJustification with UUID {justification_uuid} does not exist."
        )
        return

    verification = justification.verification
    user = justification.user

    link_to_homeport_dashboard = core_utils.format_homeport_link(
        "profile/onboarding-applications/"
    )

    context = {
        "user_full_name": user.full_name,
        "organization_name": verification.legal_name,
        "created_at": verification.created.strftime("%Y-%m-%d %H:%M"),
        "site_name": config.SITE_NAME,
        "link_to_homeport_dashboard": link_to_homeport_dashboard,
    }

    logger.info(
        f"Sending justification review notification to user {user.email} "
        f"for justification {justification_uuid}"
    )

    core_utils.broadcast_mail(
        "onboarding",
        "justification_review_notification",
        context,
        [user.email],
    )
