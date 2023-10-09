from django.db import migrations


def create_customer_role(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')

    Role = apps.get_model('permissions', 'Role')
    Customer = apps.get_model('structure', 'Customer')
    Role.objects.create(
        name='CUSTOMER.READER',
        description='Organization reader',
        content_type=ContentType.objects.get_for_model(Customer),
        is_system_role=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('permissions', '0007_role_is_active'),
    ]

    operations = [migrations.RunPython(create_customer_role)]
