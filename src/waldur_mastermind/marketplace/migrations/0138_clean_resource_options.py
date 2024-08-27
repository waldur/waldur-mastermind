from django.db import migrations


def clean_resource_options(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.all():
        if offering.resource_options and "options" in offering.resource_options:
            required_options = {
                key: {**option, "required": False}
                for key, option in offering.resource_options["options"].items()
                if option.get("required")
            }
            if required_options:
                offering.resource_options["options"] = {
                    **offering.resource_options["options"],
                    **required_options,
                }
                offering.save(update_fields=["resource_options"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0137_alter_order_state"),
    ]

    operations = [migrations.RunPython(clean_resource_options)]
