from django.db import migrations

MANAGED_RANCHER_PLUGIN = "Marketplace.ManagedRancher"

FIELD_NAME = "managed_rancher_load_balancer_cloud_init_template"


def move_cloud_init_template(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")

    for offering in Offering.objects.filter(type=MANAGED_RANCHER_PLUGIN):
        if FIELD_NAME not in offering.plugin_options:
            continue
        if FIELD_NAME in offering.secret_options:
            continue
        offering.secret_options[FIELD_NAME] = offering.plugin_options.get(FIELD_NAME)
        offering.save()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0164_customerserviceaccount_preferred_identifier"),
    ]

    operations = [
        migrations.RunPython(
            code=move_cloud_init_template,
        )
    ]
