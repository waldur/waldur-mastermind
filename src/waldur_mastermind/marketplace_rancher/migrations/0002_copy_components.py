from django.db import migrations

from waldur_mastermind.marketplace_openstack.const import TENANT_COMPONENTS
from waldur_mastermind.marketplace_rancher import MANAGED_RANCHER_PLUGIN


def copy_offering_components(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")

    for rancher_offering in Offering.objects.filter(type=MANAGED_RANCHER_PLUGIN):
        for component_data in TENANT_COMPONENTS:
            if not OfferingComponent.objects.filter(
                offering=rancher_offering,
                type=component_data.type,
            ).exists():
                # Create a new component only if it does not already exist
                # to avoid duplicates.
                OfferingComponent.objects.create(
                    offering=rancher_offering,
                    **component_data._asdict(),
                )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_rancher", "0001_initial"),
    ]

    operations = [migrations.RunPython(copy_offering_components)]
