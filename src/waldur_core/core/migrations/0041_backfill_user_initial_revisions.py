from django.db import migrations

BATCH_SIZE = 500


def backfill_user_initial_revisions(apps, schema_editor):
    """Create initial reversion snapshots for users that predate the
    version-history signal handler and therefore have no history at all.

    Mirrors marketplace migration 0205, which did the same for Resource,
    Offering and Plan but left core.User out. Without a baseline the history
    API shows a user's first *change* as its earliest known state, so there is
    nothing to compare that change against.

    Uses apps.get_model() (historical models) instead of live model imports so
    that queries only reference columns that exist at this migration state, and
    _base_manager so that inactive users - which the live default manager hides
    - are backfilled too.
    """
    from django.core import serializers as core_serializers
    from django.utils import timezone

    ContentType = apps.get_model("contenttypes", "ContentType")
    Revision = apps.get_model("reversion", "Revision")
    Version = apps.get_model("reversion", "Version")
    User = apps.get_model("core", "User")

    db_alias = schema_editor.connection.alias
    content_type = ContentType.objects.get_for_model(User)
    now = timezone.now()

    versioned_ids = set(
        Version.objects.filter(content_type=content_type)
        .values_list("object_id", flat=True)
        .distinct()
    )

    def flush(batch):
        # Bulk inserted in batches: the user table is typically the largest
        # versioned one, and a row-by-row backfill costs two round trips each.
        revisions = Revision.objects.bulk_create(
            [
                Revision(date_created=now, comment="Initial version (backfill)")
                for _ in batch
            ]
        )
        Version.objects.bulk_create(
            [
                Version(
                    revision=revision,
                    content_type=content_type,
                    object_id=str(user.pk),
                    db=db_alias,
                    format="json",
                    serialized_data=core_serializers.serialize("json", [user]),
                    object_repr=user.username,
                )
                for user, revision in zip(batch, revisions)
            ]
        )

    batch = []
    for user in User._base_manager.iterator(chunk_size=BATCH_SIZE):
        if str(user.pk) in versioned_ids:
            continue
        batch.append(user)
        if len(batch) >= BATCH_SIZE:
            flush(batch)
            batch = []
    if batch:
        flush(batch)


class Migration(migrations.Migration):
    # Each batch commits on its own rather than holding one transaction open
    # over the whole user table: the backfill is idempotent (it skips users that
    # already have a version), so an interrupted run simply resumes.
    atomic = False

    dependencies = [
        ("core", "0040_personalaccesstoken_allowed_networks"),
        ("reversion", "0001_squashed_0004_auto_20160611_1202"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(
            backfill_user_initial_revisions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
