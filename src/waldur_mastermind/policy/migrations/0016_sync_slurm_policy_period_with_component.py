"""Sync SlurmPeriodicUsagePolicy.period with OfferingComponent.limit_period.

Fixes any existing policies where the period field diverges from the
offering's limit-based component limit_period.
"""

from django.db import migrations

LIMIT_PERIOD_MAP = {
    "month": 2,  # PeriodMixin.Periods.MONTH_1
    "quarterly": 3,  # PeriodMixin.Periods.MONTH_3
    "annual": 4,  # PeriodMixin.Periods.MONTH_12
    "total": 1,  # PeriodMixin.Periods.TOTAL
}


def sync_policy_periods(apps, schema_editor):
    SlurmPeriodicUsagePolicy = apps.get_model("policy", "SlurmPeriodicUsagePolicy")
    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")

    for policy in SlurmPeriodicUsagePolicy.objects.all():
        component = OfferingComponent.objects.filter(
            offering=policy.scope,
            billing_type="limit",
        ).first()
        if not component or not component.limit_period:
            continue
        expected = LIMIT_PERIOD_MAP.get(component.limit_period)
        if expected and policy.period != expected:
            policy.period = expected
            policy.save(update_fields=["period"])


class Migration(migrations.Migration):
    dependencies = [
        ("policy", "0015_rename_fairshare_decay_half_life_to_carryover_factor"),
        ("marketplace", "0219_courseaccount_pending_state"),
    ]

    operations = [
        migrations.RunPython(sync_policy_periods, migrations.RunPython.noop),
    ]
