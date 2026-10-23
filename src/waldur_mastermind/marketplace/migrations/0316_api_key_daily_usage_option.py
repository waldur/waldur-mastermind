from django.db import migrations

DAILY_USAGE = "api_key_daily_usage"


def declare_daily_usage(apps, schema_editor):
    """Keep day and week key limits where the agent already reports per day.

    Keys are offered limits other than month only on an offering that sets
    ``api_key_daily_usage``. An offering whose keys already have daily usage
    rows has an agent that reports it, so it gets the option, and its keys keep
    being offered the periods they were. An offering that set the option either
    way is left alone.
    """
    Offering = apps.get_model("marketplace", "Offering")
    ResourceApiKeyDailyUsage = apps.get_model("marketplace", "ResourceApiKeyDailyUsage")
    offering_ids = (
        ResourceApiKeyDailyUsage.objects.values_list(
            "api_key__resource__offering_id", flat=True
        )
        .order_by()
        .distinct()
    )
    for offering in Offering.objects.filter(pk__in=list(offering_ids)):
        plugin_options = dict(offering.plugin_options or {})
        if DAILY_USAGE in plugin_options:
            continue
        plugin_options[DAILY_USAGE] = True
        Offering.objects.filter(pk=offering.pk).update(plugin_options=plugin_options)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0315_resource_api_key_monthly_limits"),
    ]

    operations = [
        migrations.RunPython(declare_daily_usage, migrations.RunPython.noop),
    ]
