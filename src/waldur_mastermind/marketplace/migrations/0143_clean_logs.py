from django.db import migrations


def clean_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    Event.objects.filter(message__contains="current_usages").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0142_resource_requested_pausing"),
    ]

    operations = [
        migrations.RunPython(clean_logs),
    ]
