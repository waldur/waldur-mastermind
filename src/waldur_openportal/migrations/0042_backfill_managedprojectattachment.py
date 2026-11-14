import uuid

from django.db import migrations
from django.db.models import Q

ATTACHED_NOTE = (
    "Reconstructed from the audit log: the award was attached to this "
    "project through the API."
)
CREATED_NOTE = (
    "Reconstructed: no recorded attach, so the award is taken to have held "
    "this project since the project was created."
)


def backfill_attachments(apps, schema_editor):
    """
    Give every award (ManagedProject) that holds a project an open attachment
    to it, so its credit can be computed from the spend made while it was
    attached.

    An explicit attach through the API is in the audit log, naming the
    project; the earliest such entry for the current project is when the
    award arrived. Without one, the award is taken to have held the project
    from the project's creation: its credit then counts all of the project's
    spend, exactly as before, and does not change on upgrade.

    Idempotent: awards that already have attachments are skipped.
    """
    ManagedProject = apps.get_model("waldur_openportal", "ManagedProject")
    ManagedProjectAttachment = apps.get_model(
        "waldur_openportal", "ManagedProjectAttachment"
    )
    ManagedProjectAuditEntry = apps.get_model(
        "waldur_openportal", "ManagedProjectAuditEntry"
    )

    managed_projects = ManagedProject.objects.filter(
        project__isnull=False, attachments__isnull=True
    ).select_related("project")

    batch = []
    for managed_project in managed_projects.iterator(chunk_size=500):
        project = managed_project.project
        project_uuid = uuid.UUID(str(project.uuid))

        attach_entry = (
            ManagedProjectAuditEntry.objects.filter(
                managed_project=managed_project,
                event_type="project_attached",
            )
            .filter(
                Q(note__contains=project_uuid.hex) | Q(note__contains=str(project_uuid))
            )
            .order_by("timestamp")
            .first()
        )

        if attach_entry is not None:
            attached_at, note = attach_entry.timestamp, ATTACHED_NOTE
        else:
            attached_at, note = project.created, CREATED_NOTE

        batch.append(
            ManagedProjectAttachment(
                managed_project=managed_project,
                project=project,
                attached_at=attached_at,
                note=note,
            )
        )
        if len(batch) >= 500:
            ManagedProjectAttachment.objects.bulk_create(batch)
            batch = []

    if batch:
        ManagedProjectAttachment.objects.bulk_create(batch)


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_openportal", "0041_managedprojectattachment"),
    ]

    operations = [
        migrations.RunPython(backfill_attachments, migrations.RunPython.noop),
    ]
