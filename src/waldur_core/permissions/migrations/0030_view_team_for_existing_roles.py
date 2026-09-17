from django.apps import apps as global_apps
from django.db import migrations

# Before CUSTOMER.VIEW_TEAM / PROJECT.VIEW_TEAM existed, every organization or
# project role let its holder list the team. 0029 kept that for the built-in
# roles and their clones; this keeps it for every other role that exists at
# upgrade time, e.g. roles staff created by hand. Roles created afterwards get
# the permission only when someone gives it to them.
#
# SRAM placeholder roles are the exception: they are meant to be private.
VIEW_TEAM = {
    "customer": "CUSTOMER.VIEW_TEAM",
    "project": "PROJECT.VIEW_TEAM",
}


def _sram_placeholder_ids(apps, schema_editor):
    # waldur_sram is an optional extension and may not be migrated yet. A failed
    # query would abort the migration's transaction, so check the table first.
    if not global_apps.is_installed("waldur_sram"):
        return set()
    try:
        SramGroup = apps.get_model("waldur_sram", "SramGroup")
    except LookupError:
        return set()
    tables = schema_editor.connection.introspection.table_names()
    if SramGroup._meta.db_table not in tables:
        return set()
    columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(), SramGroup._meta.db_table
        )
    }
    if "role_id" not in columns:
        return set()
    return set(SramGroup.objects.exclude(role=None).values_list("role_id", flat=True))


def add_view_team_to_existing_roles(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    placeholders = _sram_placeholder_ids(apps, schema_editor)
    for scope, permission in VIEW_TEAM.items():
        roles = Role.objects.filter(
            content_type__app_label="structure", content_type__model=scope
        ).exclude(id__in=placeholders)
        existing = set(
            RolePermission.objects.filter(
                role__in=roles, permission=permission
            ).values_list("role_id", flat=True)
        )
        RolePermission.objects.bulk_create(
            [
                RolePermission(role_id=role_id, permission=permission)
                for role_id in roles.values_list("id", flat=True)
                if role_id not in existing
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0029_view_team_permissions"),
    ]

    operations = [
        migrations.RunPython(add_view_team_to_existing_roles, migrations.RunPython.noop)
    ]
