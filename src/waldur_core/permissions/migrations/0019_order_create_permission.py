from django.db import migrations


def add_order_create_permission(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    PERMISSION = "ORDER.CREATE"
    ROLES = [
        "CUSTOMER.OWNER",
        "PROJECT.ADMIN",
        "PROJECT.MANAGER",
        "PROJECT.MEMBER",
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
            "0018_alter_role_description_alter_role_description_ar_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(add_order_create_permission, migrations.RunPython.noop)
    ]
