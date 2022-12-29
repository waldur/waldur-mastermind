from celery import shared_task
from django.utils import timezone

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
    broadcast_message.state = models.BroadcastMessage.States.SENT
    if not broadcast_message.send_at:
        broadcast_message.send_at = timezone.now()
    broadcast_message.save()


@shared_task(name='waldur_mastermind.notifications.send_scheduled_broadcast_messages')
def send_scheduled_broadcast_messages():
    messages = models.BroadcastMessage.objects.filter(
        state=models.BroadcastMessage.States.SCHEDULED, send_at__lte=timezone.now()
    )
    for message in messages:
        send_broadcast_message_email.delay(message.uuid.hex)
