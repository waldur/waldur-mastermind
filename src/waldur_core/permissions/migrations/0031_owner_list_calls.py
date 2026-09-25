from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml. CUSTOMER.OWNER carries the
# call team-management permissions but lacked CALL.LIST, which the call queryset
# requires of organization-level roles, so owners could not see the calls those
# permissions apply to. import_roles fixes the system role on the next deploy,
# but clones (CUSTOMER.<slug>.OWNER) copied their template's permissions once
# and never see later additions, and stacks that skip initdb never run
# import_roles at all.
PERMISSION = "CALL.LIST"
TEMPLATE = "CUSTOMER.OWNER"


def add_list_calls_to_owners(apps, schema_editor):
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
        ("permissions", "0030_view_team_for_existing_roles"),
    ]

    operations = [
        migrations.RunPython(add_list_calls_to_owners, migrations.RunPython.noop)
    ]
