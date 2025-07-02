import re

from django.db import migrations


def cleanup_noisy_resource_update_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    pattern = re.compile(
        r"'(?P<field>[^']+)': from (?P<from>[^ ]+) to (?P<to>[^ ,\.]+)"
    )

    # Get total count for progress tracking
    qs = Event.objects.filter(event_type="marketplace_resource_update_succeeded")
    total_events = qs.count()
    print(f"Starting cleanup for {total_events} events...")

    processed = 0
    deleted = 0
    batch_size = 1000
    ids_to_delete = []

    for event in qs.iterator(chunk_size=batch_size):
        # Try to find all field changes in the message
        matches = pattern.findall(event.message)
        if matches and all(f == t for _, f, t in matches):
            ids_to_delete.append(event.id)

        processed += 1

        # Delete in batches for better performance
        if len(ids_to_delete) >= batch_size:
            Event.objects.filter(id__in=ids_to_delete).delete()
            deleted += len(ids_to_delete)
            print(
                f"Deleted batch of {len(ids_to_delete)} events (total deleted: {deleted})"
            )
            ids_to_delete = []

        # Print progress every 1000 records
        if processed % 1000 == 0:
            percentage = (processed / total_events) * 100
            print(
                f"Processed {processed}/{total_events} events ({percentage:.1f}%), deleted {deleted}"
            )

    # Delete any remaining events
    if ids_to_delete:
        Event.objects.filter(id__in=ids_to_delete).delete()
        deleted += len(ids_to_delete)
        print(f"Deleted final batch of {len(ids_to_delete)} events")

    print(f"Cleanup completed! Processed {processed} events, deleted {deleted} total.")


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0165_cloud_init"),
    ]

    operations = [
        migrations.RunPython(cleanup_noisy_resource_update_logs),
    ]
