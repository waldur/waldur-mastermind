import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.media.validators import parse_email_list

from . import enums
from .models import OnboardingJustification, OnboardingVerification

logger = logging.getLogger(__name__)

# Homeport attaches the documents in separate requests right after creating a
# justification, so reviewers are told only once those have had time to land.
JUSTIFICATION_SUBMITTED_NOTIFICATION_DELAY = 60  # seconds


def notification_is_enabled(key):
    """Whether broadcast_mail would send ``key``; warn if it would not.

    broadcast_mail only logs a disabled notification at INFO, but an operator
    who filled in ONBOARDING_NOTIFICATION_EMAILS asked for these emails and
    needs a hint why none arrive. As for send_version_update_notification.
    """
    if core_models.Notification.objects.filter(key=key, enabled=True).exists():
        return True
    logger.warning(
        "ONBOARDING_NOTIFICATION_EMAILS is set, but notification '%s' is missing "
        "or disabled, so no email was sent. Enable it under Administration -> "
        "Notifications or in notifications.json.",
        key,
    )
    return False


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
        return

    # Mark each verification as deleted by task before deletion
    # This allows the pre_delete signal to log it as a task deletion
    for verification in old_verifications:
        verification._deleted_by_task = True
        verification._deleted_by = None
        verification.delete()

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


@shared_task(name="waldur_core.onboarding.send_justification_submitted_notification")
def send_justification_submitted_notification(justification_uuid):
    """
    Tell ONBOARDING_NOTIFICATION_EMAILS that a justification awaits review.

    The email carries only a link to the staff review page: the addresses may
    be a shared mailbox, so no applicant or organization data goes into it.
    Skipped if the justification was deleted or already reviewed meanwhile.
    """
    # The address list is the on/off switch; read it when sending, so emptying
    # it also stops mails already queued.
    recipients = parse_email_list(config.ONBOARDING_NOTIFICATION_EMAILS)
    if not recipients:
        return

    justification = OnboardingJustification.objects.filter(
        uuid=justification_uuid,
        validation_decision=enums.ReviewDecision.PENDING,
    ).first()
    if justification is None:
        logger.info(
            "OnboardingJustification %s is gone or already reviewed, "
            "so reviewers are not notified.",
            justification_uuid,
        )
        return

    # One mail per pending review: a resubmission (homeport submits again when
    # a document upload failed) adds a justification to a verification whose
    # earlier one already told the reviewers and still awaits them.
    if OnboardingJustification.objects.filter(
        verification_id=justification.verification_id,
        validation_decision=enums.ReviewDecision.PENDING,
        id__lt=justification.id,
    ).exists():
        return

    if not notification_is_enabled("onboarding.justification_submitted"):
        return

    context = {
        "created_at": justification.created.strftime("%Y-%m-%d %H:%M"),
        "site_name": config.SITE_NAME,
        "review_url": core_utils.format_homeport_link(
            "support/onboarding/justifications/{uuid}/",
            uuid=justification.uuid.hex,
        ),
    }
    core_utils.broadcast_mail(
        "onboarding", "justification_submitted", context, recipients
    )


@shared_task(
    name="waldur_core.onboarding.send_customer_created_automatically_notification"
)
def send_customer_created_automatically_notification(verification_uuid):
    """
    Tell ONBOARDING_NOTIFICATION_EMAILS that an organization was created after
    passing automatic validation, so staff hear of organizations nobody reviewed.

    Skipped when any justification exists: then staff reviewed the application
    (manual approval, or escalation after the register confirmed it) and
    already know. Like the justification email, carries no applicant or
    organization data, only the register's name and a link.
    """
    recipients = parse_email_list(config.ONBOARDING_NOTIFICATION_EMAILS)
    if not recipients:
        return

    verification = (
        OnboardingVerification.objects.filter(
            uuid=verification_uuid, customer__isnull=False
        )
        .exclude(validation_method="")
        .select_related("customer")
        .first()
    )
    if verification is None or verification.justifications.exists():
        return

    if not notification_is_enabled("onboarding.customer_created_automatically"):
        return

    customer = verification.customer
    context = {
        "created_at": customer.created.strftime("%Y-%m-%d %H:%M"),
        "validation_method": verification.get_validation_method_display(),
        "site_name": config.SITE_NAME,
        "organization_url": core_utils.format_homeport_link(
            "organizations/{uuid}/", uuid=customer.uuid.hex
        ),
    }
    core_utils.broadcast_mail(
        "onboarding", "customer_created_automatically", context, recipients
    )
