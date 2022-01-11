import logging
from smtplib import SMTPException

from celery import shared_task

from waldur_core.core.utils import send_mail

logger = logging.getLogger(__name__)


@shared_task
def send_mail_notification(subject, message, to):
    try:
        send_mail(subject, message, [to])
    except SMTPException:
        message = 'Failed to send email. Receiver email: %s.' % to
        logger.warning(message)
