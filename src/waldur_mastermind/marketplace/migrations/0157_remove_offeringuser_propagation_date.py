from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        (
            "marketplace",
            "0156_move_service_provider_can_create_offering_user_to_plugin_options",
        ),
    ]

    operations = [
        migrations.RemoveField(
            model_name="offeringuser",
            name="propagation_date",
        ),
    ]
