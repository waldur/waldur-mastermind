from django.db import migrations

# The type as 0322 left it; frozen like the keys below.
SITE_AGENT_OFFERING = "Marketplace.SiteAgent"

# The settings the site agent configuration generator read from plugin_options,
# where no serializer declares them, so the API could not set them. Frozen here
# rather than imported: a migration must not change when app code does.
LEGACY_KEYS = (
    "default_account",
    "customer_prefix",
    "project_prefix",
    "allocation_prefix",
    "qos_downscaled",
    "qos_paused",
    "qos_default",
    "hostname",
    "enable_user_homedir_account_creation",
)


def move_to_agent_configuration(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.filter(type=SITE_AGENT_OFFERING).only(
        "id", "plugin_options", "agent_configuration"
    ):
        # An import used to store plugin_options unvalidated, so a row can hold
        # something other than a mapping; it has no legacy keys to move.
        if not isinstance(offering.plugin_options or {}, dict):
            continue
        plugin_options = dict(offering.plugin_options or {})
        moved = {
            key: plugin_options.pop(key) for key in LEGACY_KEYS if key in plugin_options
        }
        if not moved:
            continue
        agent_configuration = dict(offering.agent_configuration or {})
        settings = dict(agent_configuration.get("backend_settings") or {})
        for key, value in moved.items():
            settings.setdefault(key, value)
        agent_configuration["backend_settings"] = settings
        offering.plugin_options = plugin_options
        offering.agent_configuration = agent_configuration
        offering.save(update_fields=["plugin_options", "agent_configuration"])


def move_back_to_plugin_options(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.filter(type=SITE_AGENT_OFFERING).only(
        "id", "plugin_options", "agent_configuration"
    ):
        settings = (offering.agent_configuration or {}).get("backend_settings") or {}
        moved = {key: settings[key] for key in LEGACY_KEYS if key in settings}
        if not moved or not isinstance(offering.plugin_options or {}, dict):
            continue
        plugin_options = dict(offering.plugin_options or {})
        for key, value in moved.items():
            plugin_options.setdefault(key, value)
        offering.plugin_options = plugin_options
        offering.save(update_fields=["plugin_options"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0327_offering_agent_configuration"),
    ]

    operations = [
        migrations.RunPython(move_to_agent_configuration, move_back_to_plugin_options),
    ]
