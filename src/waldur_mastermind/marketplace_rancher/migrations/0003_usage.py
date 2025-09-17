from django.db import migrations

from waldur_mastermind.marketplace.enums import RANCHER_OFFERING, BillingTypes


def update_billing_type_to_usage(apps, schema_editor):
    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")

    OfferingComponent.objects.filter(
        offering__type=RANCHER_OFFERING,
        billing_type=BillingTypes.LIMIT,
    ).update(billing_type=BillingTypes.USAGE)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_rancher", "0002_copy_components"),
    ]

    operations = [migrations.RunPython(update_billing_type_to_usage)]
