from django.db import migrations

# Mirrors docker/rootfs/etc/waldur/permissions.yaml. INVOICE.LIST is what the
# invoice querysets now require of organization roles, where any role on the
# organization used to be enough. import_roles fixes the system roles on the
# next deploy, but clones (CUSTOMER.<slug>.OWNER) copied their template's
# permissions once and never see later additions, and stacks that skip initdb
# never run import_roles at all. Without this, holders of these roles would
# lose the invoices they could read before.
PERMISSION = "INVOICE.LIST"
TEMPLATES = ("CUSTOMER.OWNER", "CUSTOMER.SUPPORT", "CUSTOMER.READER")


def grant_invoice_list(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    RolePermission = apps.get_model("permissions", "RolePermission")
    roles = Role.objects.filter(
        content_type__app_label="structure",
        content_type__model="customer",
        is_system_role=True,
        name__in=TEMPLATES,
    )
    clones = Role.objects.filter(
        content_type__app_label="structure",
        content_type__model="customer",
        template__name__in=TEMPLATES,
        template__is_system_role=True,
    )
    for role in roles.union(clones):
        RolePermission.objects.get_or_create(role=role, permission=PERMISSION)


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0037_project_metadata_permission"),
    ]

    operations = [migrations.RunPython(grant_invoice_list, migrations.RunPython.noop)]
