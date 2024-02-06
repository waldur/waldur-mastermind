from django.db import migrations

ROLES_MAP = {
    "owner": "CUSTOMER.OWNER",
    "service_manager": "CUSTOMER.MANAGER",
    "manager": "PROJECT.MANAGER",
    "admin": "PROJECT.ADMIN",
    "member": "PROJECT.MEMBER",
}


def fill_data(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Role = apps.get_model("permissions", "Role")
    GroupInvitation = apps.get_model("users", "GroupInvitation")
    Invitation = apps.get_model("users", "Invitation")
    for model in (GroupInvitation, Invitation):
        for obj in model.objects.all():
            obj.content_type = ContentType.objects.get_for_model(
                obj.project or obj.customer
            )
            obj.object_id = obj.project_id or obj.customer_id
            obj.role = Role.objects.get(
                name=ROLES_MAP[obj.project_role or obj.customer_role]
            )
            obj.save()


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0004_generic_invitation"),
        ("permissions", "0013_alter_rolepermission_unique_together"),
    ]

    operations = [migrations.RunPython(fill_data)]
