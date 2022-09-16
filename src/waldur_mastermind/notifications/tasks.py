from celery import shared_task

from waldur_core.core.utils import send_mail

from . import models


@shared_task(name='waldur_mastermind.notifications.send_broadcast_message_email')
def send_broadcast_message_email(broadcast_message_uuid):
    broadcast_message = models.BroadcastMessage.objects.get(uuid=broadcast_message_uuid)
    for email in broadcast_message.emails:
        send_mail(
            broadcast_message.subject,
            broadcast_message.body,
            [email],
            fail_silently=True,
        )
