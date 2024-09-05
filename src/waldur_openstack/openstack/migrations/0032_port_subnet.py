from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def fill_port_subnet(apps, schema_editor):
    Port = apps.get_model("openstack", "Port")
    SubNet = apps.get_model("openstack", "SubNet")

    for port in Port.objects.filter(subnet__isnull=True):
        try:
            subnet_id = port.fixed_ips[0]["subnet_id"]
        except (AttributeError, IndexError):
            continue
        try:
            subnet = SubNet.objects.get(backend_id=subnet_id)
        except ObjectDoesNotExist:
            print(
                f"Skipping port subnet because it is not found. Subnet backend id: {subnet_id}"
            )
            pass
        else:
            port.subnet = subnet
            port.save(update_fields=["subnet"])


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0031_port_data"),
    ]

    operations = [
        migrations.RunPython(fill_port_subnet),
    ]
