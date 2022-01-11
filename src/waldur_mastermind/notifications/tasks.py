from celery import shared_task

from waldur_core.core.utils import send_mail

from . import models


@shared_task(name='waldur_mastermind.notifications.send_notification_email')
def send_notification_email(notification_uuid):
    notification = models.Notification.objects.get(uuid=notification_uuid)
    for email in notification.emails:
        send_mail(
            notification.subject, notification.body, [email], fail_silently=True,
        )
