from django.db import migrations

BATCH_SIZE = 500


def backfill_project_initial_revisions(apps, schema_editor):
    """Create initial reversion snapshots for projects that predate project
    version history and therefore have no history at all.

    Follows core migration 0041, which did the same for users. Without a
    baseline the history API shows a project's first *change* as its earliest
    known state, so there is nothing to compare that change against.

    Uses apps.get_model() (historical models) instead of live model imports so
    that queries only reference columns that exist at this migration state, and
    _base_manager so that soft-deleted projects - which the live
    available_objects manager hides - are backfilled too.
    """
    from django.core import serializers as core_serializers
    from django.db import transaction
    from django.utils import timezone

    ContentType = apps.get_model("contenttypes", "ContentType")
    Revision = apps.get_model("reversion", "Revision")
    Version = apps.get_model("reversion", "Version")
    Project = apps.get_model("structure", "Project")

    db_alias = schema_editor.connection.alias
    content_type = ContentType.objects.get_for_model(Project)
    now = timezone.now()

    versioned_ids = set(
        Version.objects.filter(content_type=content_type)
        .values_list("object_id", flat=True)
        .distinct()
    )

    def flush(batch):
        # Wrapped in atomic() so an interruption never leaves a Revision without
        # its matching Version - the project-level idempotency check above only
        # covers projects that already got both rows.
        with transaction.atomic(using=db_alias):
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
                        object_id=str(project.pk),
                        db=db_alias,
                        format="json",
                        serialized_data=core_serializers.serialize("json", [project]),
                        # Historical models carry no __str__; this is what the
                        # live Project.__str__ renders.
                        object_repr=f"{project.name} | {project.customer.name}",
                    )
                    for project, revision in zip(batch, revisions)
                ]
            )

    connection = schema_editor.connection
    # Project._base_manager.iterator() opens a named server-side cursor. Behind
    # a transaction-mode pooler (e.g. the Zalando operator's built-in PgBouncer
    # sidecar) it can be routed to a different backend between DECLARE and
    # FETCH/CLOSE, raising psycopg.errors.InvalidCursorName. Force client-side
    # cursors for the duration of this migration only.
    original_disable_server_side_cursors = connection.settings_dict.get(
        "DISABLE_SERVER_SIDE_CURSORS"
    )
    connection.settings_dict["DISABLE_SERVER_SIDE_CURSORS"] = True
    try:
        batch = []
        projects = Project._base_manager.select_related("customer").order_by("pk")
        for project in projects.iterator(chunk_size=BATCH_SIZE):
            if str(project.pk) in versioned_ids:
                continue
            batch.append(project)
            if len(batch) >= BATCH_SIZE:
                flush(batch)
                batch = []
        if batch:
            flush(batch)
    finally:
        connection.settings_dict["DISABLE_SERVER_SIDE_CURSORS"] = (
            original_disable_server_side_cursors
        )


class Migration(migrations.Migration):
    # Each batch commits on its own rather than holding one transaction open
    # over the whole project table: the backfill is idempotent (it skips
    # projects that already have a version), so an interrupted run resumes.
    atomic = False

    dependencies = [
        ("structure", "0087_customer_invoice_emails"),
        ("reversion", "0001_squashed_0004_auto_20160611_1202"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(
            backfill_project_initial_revisions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
