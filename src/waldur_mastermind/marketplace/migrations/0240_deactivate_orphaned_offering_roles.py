from django.db import migrations
from django.utils import timezone


def deactivate_orphaned_offering_roles(apps, schema_editor):
    """One-time cleanup of roles scoped to an offering that no longer exists.

    Before the ``pre_delete`` revocation handler existed, deleting an offering
    left its Offering Manager roles active with a dangling GenericForeignKey:
    the scope resolves to ``None``, so the role shows an empty scope in the UI
    and cannot be revoked through the scope-based ``delete_user`` endpoint.
    New deletions are now handled at the source; this corrects the existing
    backlog of offering-scoped orphans.

    A bulk ``update`` is used on purpose (not ``UserRole.revoke``): the offering
    is gone, so emitting the ``role_revoked`` signal would dereference a ``None``
    scope in the audit logger.
    """
    UserRole = apps.get_model("permissions", "UserRole")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Offering = apps.get_model("marketplace", "Offering")

    try:
        offering_ct = ContentType.objects.get(app_label="marketplace", model="offering")
    except ContentType.DoesNotExist:
        return

    roles = UserRole.objects.filter(is_active=True, content_type=offering_ct)
    used_ids = list(roles.values_list("object_id", flat=True).distinct())
    live_ids = set(
        Offering._base_manager.filter(pk__in=used_ids).values_list("pk", flat=True)
    )
    orphan_ids = list(
        roles.exclude(object_id__in=live_ids).values_list("id", flat=True)
    )
    if orphan_ids:
        UserRole.objects.filter(id__in=orphan_ids).update(
            is_active=False, expiration_time=timezone.now()
        )
        print(f"Deactivated {len(orphan_ids)} orphaned offering role(s).")


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0239_offeringuser_runtime_state"),
        ("permissions", "0021_alter_role_name_roleavailability"),
    ]

    operations = [
        migrations.RunPython(
            deactivate_orphaned_offering_roles,
            migrations.RunPython.noop,
        ),
    ]
