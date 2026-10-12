from urllib.parse import urlparse

from django.db import migrations

LOGIN_NODES = "scim_login_nodes"
ENTITLEMENTS = "enable_scim_entitlements"


def _hostname_from_ssh_url(url):
    if not url or not str(url).startswith("ssh://"):
        return None
    return urlparse(url).hostname or None


def move_scim_login_nodes_off_endpoints(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    OfferingAccessEndpoint = apps.get_model("marketplace", "OfferingAccessEndpoint")
    moved = 0
    offerings = Offering.objects.filter(
        **{f"plugin_options__{ENTITLEMENTS}": True}
    ).iterator()
    for offering in offerings:
        endpoints = OfferingAccessEndpoint.objects.filter(
            offering_id=offering.pk
        ).order_by("pk")
        ssh_ids = []
        hostnames = []
        seen = set()
        for endpoint in endpoints:
            url = str(endpoint.url or "")
            if not url.startswith("ssh://"):
                continue
            ssh_ids.append(endpoint.pk)
            hostname = _hostname_from_ssh_url(url)
            if hostname and hostname not in seen:
                seen.add(hostname)
                hostnames.append(hostname)
        plugin_options = dict(offering.plugin_options or {})
        changed = False
        if hostnames and LOGIN_NODES not in plugin_options:
            plugin_options[LOGIN_NODES] = hostnames
            Offering.objects.filter(pk=offering.pk).update(
                plugin_options=plugin_options
            )
            changed = True
        if ssh_ids:
            OfferingAccessEndpoint.objects.filter(pk__in=ssh_ids).delete()
            changed = True
        if changed:
            moved += 1
    if moved:
        print(f"Moved SCIM login nodes off endpoints for {moved} offering(s).")


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0305_resourceapikey_key_ciphertext_field"),
    ]

    operations = [
        migrations.RunPython(
            move_scim_login_nodes_off_endpoints, migrations.RunPython.noop
        ),
    ]
