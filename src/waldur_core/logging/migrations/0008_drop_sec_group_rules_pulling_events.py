from django.db import migrations


def drop_events(apps, schema_editor):
    Event = apps.get_model('logging', 'Event')
    Event.objects.filter(event_type='openstack_security_group_rule_pulled').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('logging', '0007_drop_alerts'),
    ]

    operations = [migrations.RunPython(drop_events)]
