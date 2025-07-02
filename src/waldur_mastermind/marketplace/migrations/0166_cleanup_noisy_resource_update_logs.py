import re

from django.db import migrations


def cleanup_noisy_resource_update_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    step = 1000
    # Only target the relevant event type
    qs = Event.objects.filter(event_type="marketplace_resource_update_succeeded")
    pattern = re.compile(
        r"'(?P<field>[^']+)': from (?P<from>[^ ]+) to (?P<to>[^ ,\.]+)"
    )

    while True:
        batch = list(qs[:step])
        if not batch:
            break
        ids_to_delete = []
        for event in batch:
            # Try to find all field changes in the message
            matches = pattern.findall(event.message)
            if not matches:
                continue
            # If all from==to, mark for deletion
            if all(f == t for _, f, t in matches):
                ids_to_delete.append(event.id)
        if ids_to_delete:
            Event.objects.filter(id__in=ids_to_delete).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0165_cloud_init"),
    ]

    operations = [
        migrations.RunPython(cleanup_noisy_resource_update_logs),
    ]
