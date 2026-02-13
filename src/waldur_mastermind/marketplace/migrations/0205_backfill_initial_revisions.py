from django.db import migrations


def backfill_initial_revisions(apps, schema_editor):
    """Create initial reversion snapshots for existing Resources, Offerings,
    and Plans that were created before the version-history signal handler
    was deployed and therefore have no history entries.

    Uses apps.get_model() (historical models) instead of live model imports
    so that queries only reference columns that exist at this migration state.
    """
    from django.core import serializers as core_serializers

    ContentType = apps.get_model("contenttypes", "ContentType")
    Revision = apps.get_model("reversion", "Revision")
    Version = apps.get_model("reversion", "Version")

    Offering = apps.get_model("marketplace", "Offering")
    Plan = apps.get_model("marketplace", "Plan")
    Resource = apps.get_model("marketplace", "Resource")

    db_alias = schema_editor.connection.alias

    def backfill_model(Model):
        content_type = ContentType.objects.get_for_model(Model)
        versioned_ids = set(
            Version.objects.filter(content_type=content_type)
            .values_list("object_id", flat=True)
            .distinct()
        )
        for obj in Model.objects.iterator():
            if str(obj.pk) not in versioned_ids:
                revision = Revision.objects.create(
                    comment="Initial version (backfill)",
                )
                Version.objects.create(
                    revision=revision,
                    content_type=content_type,
                    object_id=str(obj.pk),
                    db=db_alias,
                    format="json",
                    serialized_data=core_serializers.serialize("json", [obj]),
                    object_repr=str(obj),
                )

    backfill_model(Resource)
    backfill_model(Offering)
    backfill_model(Plan)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0204_offeringuserattributeconfig_expose_registration_method"),
        ("reversion", "0001_squashed_0004_auto_20160611_1202"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(
            backfill_initial_revisions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
