from django.db import migrations


def backfill_notificationtemplate_initial_revisions(apps, schema_editor):
    """Create initial reversion snapshots for existing notification templates
    that predate reversion registration, so the admin history view has a
    baseline to compare a first content edit against.

    Mirrors marketplace migration 0205 and core migration 0041, which did the
    same for Resource/Offering/Plan and User respectively.
    """
    from django.core import serializers as core_serializers
    from django.utils import timezone

    ContentType = apps.get_model("contenttypes", "ContentType")
    Revision = apps.get_model("reversion", "Revision")
    Version = apps.get_model("reversion", "Version")
    NotificationTemplate = apps.get_model("core", "NotificationTemplate")

    db_alias = schema_editor.connection.alias
    content_type = ContentType.objects.get_for_model(NotificationTemplate)
    versioned_ids = set(
        Version.objects.filter(content_type=content_type)
        .values_list("object_id", flat=True)
        .distinct()
    )
    now = timezone.now()

    for template in NotificationTemplate.objects.iterator():
        if str(template.pk) in versioned_ids:
            continue
        revision = Revision.objects.create(
            date_created=now,
            comment="Initial version (backfill)",
        )
        Version.objects.create(
            revision=revision,
            content_type=content_type,
            object_id=str(template.pk),
            db=db_alias,
            format="json",
            serialized_data=core_serializers.serialize("json", [template]),
            object_repr=template.path,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0047_notificationtemplate_content"),
        ("reversion", "0001_squashed_0004_auto_20160611_1202"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(
            backfill_notificationtemplate_initial_revisions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
