from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations

COMMON_FIELDS = (
    "backend_id",
    "fixed_ips",
    "mac_address",
    "allowed_address_pairs",
    "device_id",
    "device_owner",
    "instance",
    "subnet",
)


def fill_subnet(apps, schema_editor):
    Port = apps.get_model("openstack", "Port")
    Instance = apps.get_model("openstack_tenant", "Instance")
    InternalIP = apps.get_model("openstack_tenant", "InternalIP")

    for port in (
        Port.objects.filter(device_owner__startswith="compute")
        .exclude(device_id__isnull=True)
        .exclude(device_id="")
    ):
        try:
            instance = Instance.objects.get(backend_id=port.device_id)
        except ObjectDoesNotExist:
            print(
                f"Skipping port instance because it is not found. instance backend id: {port.device_id}"
            )
            pass
        else:
            port.instance = instance
            port.save(update_fields=["instance"])

    all_ports = set(Port.objects.values_list("backend_id", flat=True))

    for internal_ip in InternalIP.objects.all():
        if internal_ip.backend_id and internal_ip.backend_id in all_ports:
            print(
                f"Skipping internal IP because it is already imported. port backend id: {internal_ip.backend_id}"
            )
            continue
        port = Port(**{key: getattr(internal_ip, key) for key in COMMON_FIELDS})
        if port.backend_id is None:
            port.backend_id = ""
        port.state = 3  # OK
        port.network_id = port.subnet.network_id
        port.tenant_id = port.subnet.tenant_id
        port.service_settings_id = port.subnet.service_settings_id
        port.project_id = port.subnet.project_id
        port.save()


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0030_port_schema"),
    ]

    operations = [
        migrations.RunPython(fill_subnet),
    ]
