from django.db import migrations

PACKAGES = 'Packages.Template'
BILLING_TYPE_FIXED = 'fixed'


def change_billing_types(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')

    for offering in Offering.objects.filter(type=PACKAGES).all():
        components = offering.components.filter(type__startswith='gigabytes_')
        if components:
            for component in components:
                component.billing_type = BILLING_TYPE_FIXED
                component.save(update_fields=['billing_type'])


class Migration(migrations.Migration):
    dependencies = [('marketplace_openstack', '0006_change_billing_type_for_volumes')]

    operations = [migrations.RunPython(change_billing_types)]
