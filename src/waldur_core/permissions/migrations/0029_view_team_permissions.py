from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml. Deployments get the system
# roles' grants from import_roles too, but clones (CUSTOMER.<slug>.OWNER, ...)
# copied their template's permissions once and never see later additions, and
# stacks that skip initdb never run import_roles at all. Without the permission
# their holders lose the team listing.
VIEW_TEAM = {
    "CUSTOMER.VIEW_TEAM": ["CUSTOMER.OWNER", "CUSTOMER.SUPPORT", "CUSTOMER.READER"],
    "PROJECT.VIEW_TEAM": ["PROJECT.ADMIN", "PROJECT.MANAGER", "PROJECT.MEMBER"],
}


def add_view_team_permissions(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    for permission, template_names in VIEW_TEAM.items():
        scope = permission.split(".")[0].lower()
        roles = Role.objects.filter(
            content_type__model=scope, is_system_role=True, name__in=template_names
        )
        clones = Role.objects.filter(
            content_type__model=scope,
            template__name__in=template_names,
            template__is_system_role=True,
        )
        for role in roles.union(clones):
            RolePermission.objects.get_or_create(role=role, permission=permission)


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0028_userrole_source"),
    ]

    operations = [
        migrations.RunPython(add_view_team_permissions, migrations.RunPython.noop)
    ]
