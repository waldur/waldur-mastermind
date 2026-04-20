from django.db import migrations


def add_set_consumer_info_permission(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    PERMISSION = "ORDER.SET_CONSUMER_INFO"
    ROLES = [
        "CUSTOMER.OWNER",
        "PROJECT.MANAGER",
    ]
    for role_name in ROLES:
        try:
            role = Role.objects.get(name=role_name)
            RolePermission.objects.get_or_create(role=role, permission=PERMISSION)
        except Role.DoesNotExist:
            pass


class Migration(migrations.Migration):
    dependencies = [
        (
            "permissions",
            "0019_order_create_permission",
        ),
    ]

    operations = [
        migrations.RunPython(
            add_set_consumer_info_permission, migrations.RunPython.noop
        )
    ]
