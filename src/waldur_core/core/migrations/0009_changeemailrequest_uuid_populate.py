import uuid

from django.db import migrations


def gen_uuid(apps, schema_editor):
    ChangeEmailRequest = apps.get_model('core', 'ChangeEmailRequest')
    for row in ChangeEmailRequest.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=['uuid'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_changeemailrequest_uuid'),
    ]

    operations = [
        migrations.RunPython(gen_uuid, elidable=True),
    ]
