from django.db import migrations


def populate_external_network_refs(apps, schema_editor):
    """
    For each distinct external_network_id string + settings combination,
    create an ExternalNetwork row and link the FK fields.
    """
    Tenant = apps.get_model("openstack", "Tenant")
    CustomerOpenStack = apps.get_model("openstack", "CustomerOpenStack")
    ExternalNetwork = apps.get_model("openstack", "ExternalNetwork")

    # Collect all distinct (settings, external_network_id) pairs from tenants
    for tenant in Tenant.objects.exclude(external_network_id="").select_related(
        "service_settings"
    ):
        ext_net, _ = ExternalNetwork.objects.get_or_create(
            settings=tenant.service_settings,
            backend_id=tenant.external_network_id,
            defaults={"name": tenant.external_network_id},
        )
        tenant.external_network_ref = ext_net
        tenant.save(update_fields=["external_network_ref"])

    # Link CustomerOpenStack records
    for cos in CustomerOpenStack.objects.exclude(external_network_id="").select_related(
        "settings"
    ):
        ext_net, _ = ExternalNetwork.objects.get_or_create(
            settings=cos.settings,
            backend_id=cos.external_network_id,
            defaults={"name": cos.external_network_id},
        )
        cos.external_network_ref = ext_net
        cos.save(update_fields=["external_network_ref"])

    # Create ExternalNetwork records from service settings options
    ServiceSettings = apps.get_model("structure", "ServiceSettings")
    for ss in ServiceSettings.objects.filter(type="OpenStack"):
        ext_net_id = (ss.options or {}).get("external_network_id", "")
        if ext_net_id:
            ExternalNetwork.objects.get_or_create(
                settings=ss,
                backend_id=ext_net_id,
                defaults={"name": ext_net_id},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0064_external_network_models"),
    ]

    operations = [
        migrations.RunPython(
            populate_external_network_refs,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
