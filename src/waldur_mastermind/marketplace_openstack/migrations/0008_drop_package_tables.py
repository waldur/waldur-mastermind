from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace_openstack', '0007_change_billing_type_for_volumes_of_tenants'),
        ('invoices', '0043_drop_package_column'),
        ('marketplace', '0041_drop_package'),
    ]

    operations = []
