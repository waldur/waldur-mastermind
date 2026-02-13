from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0206_order_provider_consumer_messages"),
    ]

    operations = [
        migrations.AddField(
            model_name="offering",
            name="backend_id_rules",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Validation rules for resource backend_id: format regex and uniqueness scope.",
            ),
        ),
    ]
