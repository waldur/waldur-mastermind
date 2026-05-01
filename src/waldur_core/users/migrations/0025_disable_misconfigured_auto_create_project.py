from django.db import migrations


def disable_misconfigured_auto_create_project(apps, schema_editor):
    """Legacy GroupInvitations were able to set ``auto_create_project=True``
    with a Customer-scoped ``role`` and no ``project_role``. On approval the
    code used to silently create a UserRole with a mismatched content_type
    (no actual permission). The new serializer rejects that combination;
    sweep existing rows to ``auto_create_project=False`` so they grant the
    Customer-scoped role on the Customer scope, matching what users have
    actually been receiving in practice.
    """
    GroupInvitation = apps.get_model("users", "GroupInvitation")
    affected = GroupInvitation.objects.filter(
        auto_create_project=True,
        project_role__isnull=True,
    ).exclude(role__name__startswith="PROJECT.")
    affected.update(auto_create_project=False)


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0024_alter_invitation_extra_invitation_text_max_length"),
        # Merge the orphan leaf left over from a parallel branch.
        ("users", "0013_custom_project_and_multiple_requests"),
    ]

    operations = [
        migrations.RunPython(
            disable_misconfigured_auto_create_project,
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        ),
    ]
