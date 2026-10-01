from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml. import_roles fixes the
# system role on the next deploy, but clones (CUSTOMER.<slug>.OWNER) copied
# their template's permissions once and never see later additions, and stacks
# that skip initdb never run import_roles at all. Without it, owners on a clone
# could not create their projects' Matrix rooms.
PERMISSION = "MATRIX_ROOM.CREATE"
TEMPLATE = "CUSTOMER.OWNER"


def add_create_matrix_room_to_owners(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    roles = Role.objects.filter(
        content_type__model="customer", is_system_role=True, name=TEMPLATE
    )
    clones = Role.objects.filter(
        content_type__model="customer",
        template__name=TEMPLATE,
        template__is_system_role=True,
    )
    for role in roles.union(clones):
        RolePermission.objects.get_or_create(role=role, permission=PERMISSION)


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0031_owner_list_calls"),
    ]

    operations = [
        migrations.RunPython(
            add_create_matrix_room_to_owners, migrations.RunPython.noop
        )
    ]
