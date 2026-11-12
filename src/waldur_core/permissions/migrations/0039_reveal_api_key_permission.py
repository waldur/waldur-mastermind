from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml. Revealing a resource's
# unassigned API key used to need any role on its project or organization; it
# now needs RESOURCE.REVEAL_API_KEY. import_roles fixes the system roles on the
# next deploy, but clones (CUSTOMER.<slug>.OWNER) copied their template's
# permissions once and never see later additions, and stacks that skip initdb
# never run import_roles at all. Without this, holders of these roles would lose
# the keys they could reveal before.
#
# Clones are organization roles only: a system role with a template (the
# collaborator is templated on the project member) is left to permissions.yaml,
# which withholds the permission from it on purpose.
#
# Personal access tokens are not given the permission: reveal refuses them
# whatever their scopes.
PERMISSION = "RESOURCE.REVEAL_API_KEY"
TEMPLATES = {
    "customer": ("CUSTOMER.OWNER",),
    "project": (
        "PROJECT.ADMIN",
        "PROJECT.MANAGER",
        "PROJECT.MEMBER",
        "PROJECT.PRINCIPAL_INVESTIGATOR",
        "PROJECT.CO_INVESTIGATOR",
    ),
}


def grant_reveal_api_key(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    for model, names in TEMPLATES.items():
        roles = Role.objects.filter(
            content_type__app_label="structure",
            content_type__model=model,
            is_system_role=True,
            name__in=names,
        )
        clones = Role.objects.filter(
            content_type__app_label="structure",
            content_type__model=model,
            is_system_role=False,
            template__name__in=names,
            template__is_system_role=True,
        )
        for role in roles.union(clones):
            RolePermission.objects.get_or_create(role=role, permission=PERMISSION)


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0038_invoice_list_permission"),
    ]

    operations = [migrations.RunPython(grant_reveal_api_key, migrations.RunPython.noop)]
