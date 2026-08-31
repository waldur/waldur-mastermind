import logging
from collections import defaultdict

from django.db import migrations

logger = logging.getLogger(__name__)


def dedup_notificationtemplate_paths(apps, schema_editor):
    """
    Merge duplicate NotificationTemplate rows sharing the same path, ahead of
    the unique constraint added in the next migration.

    ``path`` was never unique, and merging content onto this row (0047) turned
    a previously-survivable duplicate into a real bug: the loader's cache is
    keyed by path alone, so which duplicate's content actually gets served
    depends on cache state, and ``override_templates``' unguarded
    ``get_or_create(path=...)`` crashes outright on one.

    For each duplicate group, keep the row most likely to hold a real
    override (non-blank content, most recently modified), reassign every
    Notification.templates reference from the losing rows to it, and delete
    the losers. Reassignment skips (notification, winner) pairs that already
    exist, since the through table has its own uniqueness constraint - those
    references are simply redundant once the loser is gone.

    Kept as its own migration (rather than combined with the AlterField that
    adds the unique constraint) because Postgres refuses to ALTER TABLE in the
    same transaction as pending trigger events from the deletes below.
    """
    NotificationTemplate = apps.get_model("core", "NotificationTemplate")
    Notification = apps.get_model("core", "Notification")
    Through = Notification.templates.through

    paths = defaultdict(list)
    for row in NotificationTemplate.objects.order_by("id"):
        paths[row.path].append(row)

    for path, rows in paths.items():
        if len(rows) <= 1:
            continue

        winner, *losers = sorted(
            rows, key=lambda r: (bool(r.content), r.modified), reverse=True
        )
        logger.warning(
            "Merging %d duplicate NotificationTemplate row(s) for path '%s' into id=%d",
            len(losers),
            path,
            winner.id,
        )

        for loser in losers:
            existing_notification_ids = set(
                Through.objects.filter(notificationtemplate_id=winner.id).values_list(
                    "notification_id", flat=True
                )
            )
            Through.objects.filter(notificationtemplate_id=loser.id).exclude(
                notification_id__in=existing_notification_ids
            ).update(notificationtemplate_id=winner.id)
            # Any refs still pointing at the loser now duplicate a (notification,
            # winner) pair that already exists - drop them before the row itself.
            Through.objects.filter(notificationtemplate_id=loser.id).delete()
            loser.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0048_backfill_notificationtemplate_initial_revisions"),
    ]

    operations = [
        migrations.RunPython(
            dedup_notificationtemplate_paths, reverse_code=migrations.RunPython.noop
        ),
    ]
