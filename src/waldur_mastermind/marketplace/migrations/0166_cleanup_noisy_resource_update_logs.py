import re

from django.db import migrations


def cleanup_noisy_resource_update_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    step = 1000
    pattern = re.compile(
        r"'(?P<field>[^']+)': from (?P<from>[^ ]+) to (?P<to>[^ ,\.]+)"
    )

    # Get initial total count
    qs = Event.objects.filter(
        event_type="marketplace_resource_update_succeeded"
    ).order_by("id")
    total_count = qs.count()
    print(f"Starting cleanup: {total_count} events to process")

    total_deleted = 0
    processed_batches = 0
    offset = 0

    while offset < total_count:
        batch = list(qs[offset : offset + step])
        if not batch:
            break

        processed_batches += 1
        ids_to_delete = []

        for event in batch:
            matches = pattern.findall(event.message)
            if not matches:
                continue
            if all(f == t for _, f, t in matches):
                ids_to_delete.append(event.id)

        if ids_to_delete:
            Event.objects.filter(id__in=ids_to_delete).delete()
            batch_deleted = len(ids_to_delete)
            total_deleted += batch_deleted
            print(
                f"Batch {processed_batches}: processed {len(batch)} events, deleted {batch_deleted} (total deleted: {total_deleted})"
            )
        else:
            print(
                f"Batch {processed_batches}: processed {len(batch)} events, no deletions"
            )

        offset += step

    remaining_count = Event.objects.filter(
        event_type="marketplace_resource_update_succeeded"
    ).count()
    print(
        f"Cleanup completed: deleted {total_deleted} out of {total_count} events, {remaining_count} remaining"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0165_cloud_init"),
    ]

    operations = [
        migrations.RunPython(cleanup_noisy_resource_update_logs),
    ]
