from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml. import_roles fixes the
# system role on the next deploy, but stacks that skip initdb never run
# import_roles at all. Without these, a service provider manager sees none of
# the provider's orders and cannot approve or reject them. Only the system role
# is updated: ServiceProvider-scoped roles cannot be cloned per organization.
PERMISSIONS = ("ORDER.LIST", "ORDER.APPROVE", "ORDER.REJECT")
ROLE = "CUSTOMER.MANAGER"


def add_order_permissions_to_managers(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    roles = Role.objects.filter(
        content_type__app_label="marketplace",
        content_type__model="serviceprovider",
        is_system_role=True,
        name=ROLE,
    )
    for role in roles:
        for permission in PERMISSIONS:
            RolePermission.objects.get_or_create(role=role, permission=permission)


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0032_owner_create_matrix_room"),
    ]

    operations = [
        migrations.RunPython(
            add_order_permissions_to_managers, migrations.RunPython.noop
        )
    ]
