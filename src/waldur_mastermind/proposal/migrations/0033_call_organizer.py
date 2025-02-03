from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def upgrade_role(apps, schema_editor):
    Role = apps.get_model("permissions", "Role")
    UserRole = apps.get_model("permissions", "UserRole")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Customer = apps.get_model("structure", "Customer")
    CallManagingOrganisation = apps.get_model("proposal", "CallManagingOrganisation")

    CustomerContentType = ContentType.objects.get_for_model(Customer)
    CallManagingOrganisationContentType = ContentType.objects.get_for_model(
        CallManagingOrganisation
    )

    Role.objects.filter(name="CUSTOMER.CALL_ORGANIZER").update(
        content_type=CallManagingOrganisationContentType
    )

    for user_role in UserRole.objects.filter(
        role__name="CUSTOMER.CALL_ORGANIZER", content_type=CustomerContentType
    ):
        try:
            service_provider = CallManagingOrganisation.objects.get(
                customer_id=user_role.object_id
            )
        except ObjectDoesNotExist:
            print(
                f"CallManagingOrganisation for customer {user_role.object_id} not found"
            )
            continue
        user_role.content_type = CallManagingOrganisationContentType
        user_role.object_id = service_provider.id
        user_role.save(update_fields=["content_type", "object_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0015_customer_manager"),
        ("proposal", "0032_call_external_url"),
    ]

    operations = [
        migrations.RunPython(upgrade_role),
    ]
