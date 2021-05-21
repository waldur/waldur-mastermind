from django.db import migrations

TENANT_TYPE = 'Packages.Template'
LIMIT = 'limit'


def process_components(apps, schema_editor):
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    OfferingComponent.objects.filter(offering__type=TENANT_TYPE).update(
        billing_type=LIMIT
    )


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace_openstack', '0010_split_invoice_items'),
    ]

    operations = [migrations.RunPython(process_components)]
