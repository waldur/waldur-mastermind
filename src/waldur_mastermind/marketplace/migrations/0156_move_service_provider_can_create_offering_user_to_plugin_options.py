from django.db import migrations


def move_to_plugin_options(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.all():
        options = offering.options or {}
        plugin_options = offering.plugin_options or {}

        if "service_provider_can_create_offering_user" in options:
            plugin_options["service_provider_can_create_offering_user"] = options[
                "service_provider_can_create_offering_user"
            ]
            # Remove from options
            options.pop("service_provider_can_create_offering_user", None)

            offering.options = options
            offering.plugin_options = plugin_options
            offering.save()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0155_alter_categorycolumn_widget"),
    ]

    operations = [
        migrations.RunPython(move_to_plugin_options),
    ]
