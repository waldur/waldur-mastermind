from django.db import migrations

INSTANCE_TYPE = 'OpenStackTenant.Instance'

VOLUME_TYPE = 'OpenStackTenant.Volume'


def change_billing_types(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')

    for offering in Offering.objects.filter(
        type__in=(INSTANCE_TYPE, VOLUME_TYPE)
    ).all():
        components = offering.components.filter(type__startswith='gigabytes_')
        if components:
            for component in components:
                component.type = OfferingComponent.BillingTypes.FIXED
                component.save(update_fields=['type'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace_openstack', '0005_change_private_offerings_customers')
    ]

    operations = [migrations.RunPython(change_billing_types)]
