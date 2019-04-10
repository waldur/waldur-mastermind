from django.db import migrations
import uuid


def gen_uuid(apps, schema_editor):
    ResourcePlanPeriod = apps.get_model('marketplace', 'ResourcePlanPeriod')
    for row in ResourcePlanPeriod.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=['uuid'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0075_plan_period_uuid'),
    ]

    operations = [
        migrations.RunPython(gen_uuid),
    ]
