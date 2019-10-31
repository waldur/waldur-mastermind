import logging
from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


@shared_task
def send_mail_notification(subject, message, to):
    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [to])
    except SMTPException:
        message = 'Failed to send email. Receiver email: %s.' % to
        logger.warning(message)
