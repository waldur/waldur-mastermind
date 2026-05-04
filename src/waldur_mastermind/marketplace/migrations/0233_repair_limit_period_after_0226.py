"""Repair OfferingComponent.limit_period damaged by the original 0226 backfill.

WAL-9907. The first version of 0226_alter_offeringcomponent_limit_period
unconditionally rewrote every NULL/empty limit_period to "month", even on
offerings whose SlurmPeriodicUsagePolicy.period was MONTH_3 / MONTH_12 /
TOTAL. That silently demoted quarterly/annual offerings to monthly behavior.

0226 has since been patched to honor policy.period, but Django will not re-run
a migration whose row already exists in django_migrations — so sites that ran
the broken version (8.0.8-rc.1 .. -rc.13) need a separate repair pass.

This migration is the inverse of the bug: for every SlurmPeriodicUsagePolicy
with a non-monthly period, it rewrites LIMIT components on the linked offering
from "month" (the value 0226 wrote) to the matching policy period string.

Idempotent. Safe on fresh deployments where 0226 has already done the right
thing — those components will already match the policy.period mapping, so no
rows are updated.
"""

from django.db import migrations

POLICY_PERIOD_TO_LIMIT_PERIOD = {
    1: "total",
    3: "quarterly",
    4: "annual",
}


def repair_limit_period(apps, schema_editor):
    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")
    SlurmPeriodicUsagePolicy = apps.get_model("policy", "SlurmPeriodicUsagePolicy")

    for policy in SlurmPeriodicUsagePolicy.objects.exclude(period=2):
        derived = POLICY_PERIOD_TO_LIMIT_PERIOD.get(policy.period)
        if not derived:
            continue
        # Restrict to the value 0226 wrote — never overwrite a value the
        # operator may have set deliberately to something else.
        OfferingComponent.objects.filter(
            offering_id=policy.scope_id,
            billing_type="limit",
            limit_period="month",
        ).update(limit_period=derived)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0232_cleanup_orphan_role_availabilities"),
        ("policy", "0016_sync_slurm_policy_period_with_component"),
    ]

    operations = [
        migrations.RunPython(repair_limit_period, migrations.RunPython.noop),
    ]
