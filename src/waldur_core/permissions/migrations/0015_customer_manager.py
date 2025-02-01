from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def upgrade_role(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    UserRole = apps.get_model("permissions", "UserRole")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Customer = apps.get_model("structure", "Customer")
    ServiceProvider = apps.get_model("marketplace", "ServiceProvider")

    CustomerContentType = ContentType.objects.get_for_model(Customer)
    ServiceProviderContentType = ContentType.objects.get_for_model(ServiceProvider)

    Role.objects.filter(name="CUSTOMER.MANAGER").update(
        content_type=ServiceProviderContentType
    )

    for user_role in UserRole.objects.filter(
        role__name="CUSTOMER.MANAGER", content_type=CustomerContentType
    ):
        try:
            service_provider = ServiceProvider.objects.get(
                customer_id=user_role.object_id
            )
        except ObjectDoesNotExist:
            print(f"Service provider for user role {user_role} not found")
            continue
        user_role.content_type = ServiceProviderContentType
        user_role.object_id = service_provider.id
        user_role.save(update_fields=["content_type", "object_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0014_unify_log"),
    ]

    operations = [
        migrations.RunPython(upgrade_role),
    ]
