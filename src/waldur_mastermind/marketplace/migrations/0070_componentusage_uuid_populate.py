from django.db import migrations
import uuid


def gen_uuid(apps, schema_editor):
    ComponentUsage = apps.get_model('marketplace', 'ComponentUsage')
    for row in ComponentUsage.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=['uuid'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0069_componentusage_uuid'),
    ]

    operations = [
        migrations.RunPython(gen_uuid),
    ]
