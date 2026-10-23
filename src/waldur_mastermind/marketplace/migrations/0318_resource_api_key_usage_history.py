from collections import defaultdict
from decimal import Decimal

from django.db import migrations, models
from django.db.models import F


def _json_number(amount: Decimal):
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def backfill_usage_history(apps, schema_editor):
    # A key kept only its latest month; the months before it survive only as
    # daily rows, from the start of the previous year at most (the prune keeps
    # no older ones). Months reported only as month totals are gone.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    ResourceApiKeyDailyUsage = apps.get_model("marketplace", "ResourceApiKeyDailyUsage")
    history = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    rows = ResourceApiKeyDailyUsage.objects.filter(
        date__lt=F("api_key__usage_period")
    ).values_list("api_key_id", "component_type", "date", "usage")
    for api_key_id, component_type, date, usage in rows.iterator():
        history[api_key_id][f"{date:%Y-%m}"][component_type] += usage
    for api_key_id, months in history.items():
        ResourceApiKey.objects.filter(pk=api_key_id).update(
            usage_history={
                month: {
                    component_type: _json_number(amount)
                    for component_type, amount in usages.items()
                }
                for month, usages in sorted(months.items())
            }
        )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0317_resource_api_key_limit_pause_retries"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourceapikey",
            name="usage_history",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Usage per component type of each month before usage_period: {"2026-09": {"token_cost": 12.5}}. Kept when the next month\'s usage comes in, and raised, never lowered, by usage reported late for the month. Never pruned: a total limit counts it.',
            ),
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="usage_period",
            field=models.DateField(
                blank=True,
                help_text="The month current_usages covers (its first day). Earlier months are kept in usage_history.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="resource",
            name="usage_limit_resumes_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="When the spending limits that restrict the resource reset and the restriction lifts. Empty when no spending limit restricts the resource, when a limit-based component's limit does too, or while a reached total limit holds it: a total never resets, so the restriction lifts only when that limit is raised or cleared.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="limit_resumes_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When every limit that paused the key has reset, so that it resumes without a new usage report. Empty while a reached total limit holds the key: a total never resets, so the key resumes only when that limit is raised or cleared.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="paused_by_limit",
            field=models.BooleanField(
                default=False,
                help_text="Waldur paused the key because its usage reached a limit. Such a key is resumed automatically once its usage is under its limits again: when the period of the limit resets, or when a limit is raised. A total limit never resets, so only raising or clearing it does.",
            ),
        ),
        migrations.RunPython(backfill_usage_history, migrations.RunPython.noop),
    ]
