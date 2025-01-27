from django.db import migrations


def update_event_type(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    Event.objects.filter(event_type="marketplace_resource_has_been_changed").update(
        event_type="marketplace_resource_update_succeeded"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0149_resource_last_sync"),
    ]

    operations = [
        migrations.RunPython(update_event_type),
    ]
