from django.db import migrations

BATCH_SIZE = 2000


def backfill_project_uuid(apps, schema_editor):
    """Populate project_name/project_uuid on invoice items created through
    bulk_create, which does not fire the denormalising post_save handler.
    Credit compensation items are the only production source; without these
    columns they are invisible to the project-scoped costs endpoint, which
    filters on project_uuid rather than project_id.
    """
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")
    queryset = (
        InvoiceItem.objects.filter(project__isnull=False)
        .filter(project_uuid="")
        .select_related("project")
        .only("id", "project__uuid", "project__name")
    )
    batch = []
    for item in queryset.iterator(chunk_size=BATCH_SIZE):
        item.project_uuid = item.project.uuid.hex
        item.project_name = item.project.name
        batch.append(item)
        if len(batch) >= BATCH_SIZE:
            InvoiceItem.objects.bulk_update(
                batch, ["project_uuid", "project_name"], batch_size=BATCH_SIZE
            )
            batch = []
    if batch:
        InvoiceItem.objects.bulk_update(
            batch, ["project_uuid", "project_name"], batch_size=BATCH_SIZE
        )


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0026_credittransaction_comment_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_project_uuid, migrations.RunPython.noop),
    ]
