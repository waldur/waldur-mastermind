from django.db import migrations


def rename_internal_ips_set(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")
    for resource in Resource.objects.filter(
        attributes__has_any_keys=["internal_ips_set"]
    ):
        resource.attributes["ports"] = resource.attributes["internal_ips_set"]
        del resource.attributes["internal_ips_set"]
        resource.save(update_fields=["attributes"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_openstack", "0016_add_limit_period_to_vpc"),
    ]

    operations = [migrations.RunPython(rename_internal_ips_set)]
