from django.db import migrations


def backfill_initial_revisions(apps, schema_editor):
    """Create initial reversion snapshots for existing Resources, Offerings,
    and Plans that were created before the version-history signal handler
    was deployed and therefore have no history entries.
    """
    import reversion
    from django.contrib.contenttypes.models import ContentType
    from reversion.models import Version

    from waldur_mastermind.marketplace.models import Offering, Plan, Resource

    def get_unversioned_objects(Model):
        content_type = ContentType.objects.get_for_model(Model)
        versioned_ids = set(
            Version.objects.filter(content_type=content_type)
            .values_list("object_id", flat=True)
            .distinct()
        )
        return [
            obj for obj in Model.objects.iterator() if str(obj.pk) not in versioned_ids
        ]

    # 1. Resources – no follow relations, standalone
    for obj in get_unversioned_objects(Resource):
        with reversion.create_revision():
            reversion.add_to_revision(obj)
            reversion.set_comment("Initial version (backfill)")

    # 2. Offerings – follow=("components", "plans", "screenshots")
    #    This also creates Version rows for related Plans and their components.
    for obj in get_unversioned_objects(Offering):
        with reversion.create_revision():
            reversion.add_to_revision(obj)
            reversion.set_comment("Initial version (backfill)")

    # 3. Any remaining Plans not already covered by Offering's follow relations
    for obj in get_unversioned_objects(Plan):
        with reversion.create_revision():
            reversion.add_to_revision(obj)
            reversion.set_comment("Initial version (backfill)")


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
