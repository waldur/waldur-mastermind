from django.db import migrations
from django.utils import timezone


def fill_state(apps, schema_editor):
    BroadcastMessage = apps.get_model('notifications', 'BroadcastMessage')
    BroadcastMessage.objects.update(state='SENT', send_at=timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0005_broadcastmessage_state'),
    ]

    operations = [
        migrations.RunPython(fill_state),
    ]
