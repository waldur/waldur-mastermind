from django.db import migrations


def cleanup_orphan_role_availabilities(apps, schema_editor):
    """Remove RoleAvailability rows that bind a profile-catalog Role to an
    Offering whose current profile does not include that role.

    Such rows are leftovers from manual / pre-OfferingProfile API calls and
    cause stale roles to appear on per-offering Roles tabs.
    """
    RoleAvailability = apps.get_model("permissions", "RoleAvailability")
    OfferingProfile = apps.get_model("marketplace", "OfferingProfile")
    Offering = apps.get_model("marketplace", "Offering")
    ContentType = apps.get_model("contenttypes", "ContentType")

    offering_ct = ContentType.objects.filter(
        app_label="marketplace", model="offering"
    ).first()
    if not offering_ct:
        return

    role_to_profiles: dict[int, set[int]] = {}
    for profile in OfferingProfile.objects.prefetch_related("roles").all():
        for role in profile.roles.all():
            role_to_profiles.setdefault(role.id, set()).add(profile.id)

    if not role_to_profiles:
        return

    catalog_role_ids = list(role_to_profiles.keys())
    bindings = RoleAvailability.objects.filter(
        content_type=offering_ct, role_id__in=catalog_role_ids
    )

    to_delete: list[int] = []
    offering_profiles = dict(
        Offering.objects.filter(
            id__in=bindings.values_list("object_id", flat=True)
        ).values_list("id", "profile_id")
    )

    for binding in bindings:
        bound_profile_id = offering_profiles.get(binding.object_id)
        if bound_profile_id and bound_profile_id in role_to_profiles[binding.role_id]:
            continue
        to_delete.append(binding.id)

    if to_delete:
        RoleAvailability.objects.filter(id__in=to_delete).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0231_offeringprofile_offering_profile"),
        ("permissions", "0021_alter_role_name_roleavailability"),
    ]

    operations = [
        migrations.RunPython(
            cleanup_orphan_role_availabilities,
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        ),
    ]
