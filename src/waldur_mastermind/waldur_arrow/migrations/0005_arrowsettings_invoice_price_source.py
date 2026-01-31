from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "waldur_arrow",
            "0004_remove_arrowconsumptionrecord_vendor_subscription_id",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="arrowsettings",
            name="invoice_price_source",
            field=models.CharField(
                choices=[("sell", "Sell price"), ("buy", "Buy price")],
                default="sell",
                help_text="Which price to use for invoice items: sell or buy",
                max_length=10,
            ),
        ),
    ]
