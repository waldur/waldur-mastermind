from django.db import migrations


def limits_to_months(apps, schema_editor):
    # A key's limits were monthly, one amount per component type, and zero meant
    # no limit. Each becomes that component's month limit.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    for api_key in ResourceApiKey.objects.exclude(spending_limits__isnull=True):
        limits = api_key.spending_limits
        if not isinstance(limits, dict):
            continue
        spending_limits = {
            component_type: {"month": amount}
            for component_type, amount in limits.items()
            if isinstance(amount, int | float)
            and not isinstance(amount, bool)
            and amount
        }
        api_key.spending_limits = spending_limits or None
        api_key.save(update_fields=["spending_limits"])


def months_to_limits(apps, schema_editor):
    # Only month limits survive the way back: the old shape had no other period.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    for api_key in ResourceApiKey.objects.exclude(spending_limits__isnull=True):
        limits = {
            component_type: periods["month"]
            for component_type, periods in (api_key.spending_limits or {}).items()
            if isinstance(periods, dict) and periods.get("month") is not None
        }
        api_key.spending_limits = limits or None
        api_key.save(update_fields=["spending_limits"])


class Migration(migrations.Migration):
    # Kept apart from 0309: these UPDATEs queue deferred FK trigger events, and
    # Postgres then refuses the indexes 0309 creates in the same transaction.
    dependencies = [
        ("marketplace", "0309_resource_api_key_spending_limits"),
    ]

    operations = [
        migrations.RunPython(limits_to_months, months_to_limits),
    ]
