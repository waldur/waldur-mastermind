from django.db import migrations


def remove_duplicate_notification_templates(apps, schema_editor):
    """
    Remove duplicate NotificationTemplate rows that have the same path.
    Keeps the oldest record (smallest ID) for each unique path.
    """
    NotificationTemplate = apps.get_model("core", "NotificationTemplate")

    # Find all paths that have duplicates
    duplicate_paths = []
    all_paths = NotificationTemplate.objects.values_list("path", flat=True).distinct()

    for path in all_paths:
        path_count = NotificationTemplate.objects.filter(path=path).count()
        if path_count > 1:
            duplicate_paths.append(path)

    # For each duplicate path, keep only the oldest record (smallest ID)
    deleted_count = 0
    for path in duplicate_paths:
        templates_with_path = NotificationTemplate.objects.filter(path=path).order_by(
            "id"
        )

        if templates_with_path.count() > 1:
            # Keep the first (oldest) record, delete the rest
            templates_to_delete = templates_with_path[1:]  # Skip the first one

            for template in templates_to_delete:
                print(
                    f"Deleting duplicate NotificationTemplate: ID={template.id}, path='{template.path}', name='{template.name}'"
                )
                template.delete()
                deleted_count += 1

    print(f"Removed {deleted_count} duplicate NotificationTemplate records")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_notifications_cleanup"),
    ]

    operations = [
        migrations.RunPython(
            remove_duplicate_notification_templates,
            hints={"NotificationTemplate": "core.NotificationTemplate"},
        ),
    ]
