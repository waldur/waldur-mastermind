from django.db import migrations

ROLE_MAP = {
    ("customer", "owner"): "CUSTOMER.OWNER",
    ("customer", "service_manager"): "CUSTOMER.MANAGER",
    ("customer", "support"): "CUSTOMER.SUPPORT",
    ("project", "admin"): "PROJECT.ADMIN",
    ("project", "manager"): "PROJECT.MANAGER",
    ("project", "member"): "PROJECT.MEMBER",
    ("offering", None): "OFFERING.MANAGER",
}


def unify_log(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    for event in Event.objects.filter(
        event_type__in=["role_granted", "role_revoked", "role_updated"]
    ):
        scope_type = event.context.get("structure_type")
        if not scope_type:
            continue
        event.context["scope_type"] = scope_type
        event.context["scope_uuid"] = event.context[f"{scope_type}_uuid"]
        event.context["scope_name"] = event.context[f"{scope_type}_name"]
        old_role_name = event.context.get("role_name")
        if old_role_name:
            event.context["role_name"] = ROLE_MAP.get(
                (event.context["scope_type"], old_role_name.lower())
            )
        else:
            event.context["role_name"] = "OFFERING.MANAGER"
        del event.context["structure_type"]
        event.save()


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0013_alter_rolepermission_unique_together"),
    ]

    operations = [
        migrations.RunPython(unify_log),
    ]
