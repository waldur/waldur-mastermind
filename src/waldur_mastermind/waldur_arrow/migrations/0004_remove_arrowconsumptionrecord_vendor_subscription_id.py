from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_arrow", "0003_add_vendor_offering_mapping"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="arrowconsumptionrecord",
            name="vendor_subscription_id",
        ),
    ]
