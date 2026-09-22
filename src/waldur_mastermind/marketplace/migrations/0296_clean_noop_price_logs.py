from decimal import Decimal

from django.db import migrations

# Pinned rather than imported from waldur_mastermind.common: a historical
# migration must keep deciding what it deletes the way it did when it was
# written, whatever the application's price precision becomes later.
PRICE_EXPONENT = Decimal("1E-10")

EVENT_TYPES = [
    "marketplace_plan_component_current_price_updated",
    "marketplace_plan_component_future_price_updated",
    "marketplace_plan_component_quota_updated",
]

BATCH_SIZE = 1000


def is_noop(context):
    """A logged update that did not actually move the value."""
    old_value = context.get("old_value")
    new_value = context.get("new_value")
    if old_value is None or new_value is None:
        return False
    try:
        return Decimal(old_value).quantize(PRICE_EXPONENT) == Decimal(
            new_value
        ).quantize(PRICE_EXPONENT)
    except (ArithmeticError, TypeError, ValueError):
        # A value that does not parse as a number is left alone: it is not
        # something this migration can judge.
        return False


def clean_noop_price_logs(apps, schema_editor):
    """
    Delete price and quota events that recorded no actual change.

    Until the emitting handler learned to compare values as numbers, every
    offering sync re-logged each component whose price came back from the
    remote API as a string -- the same price, a different representation.
    Migration 0145 cleared the ones written before it; this clears those
    written since, and the quota events it did not cover.
    """
    Event = apps.get_model("logging", "Event")
    queryset = Event.objects.filter(event_type__in=EVENT_TYPES)

    doomed = []
    for event in queryset.only("id", "context").iterator(chunk_size=BATCH_SIZE):
        if is_noop(event.context):
            doomed.append(event.id)
            if len(doomed) >= BATCH_SIZE:
                Event.objects.filter(id__in=doomed).delete()
                doomed = []
    if doomed:
        Event.objects.filter(id__in=doomed).delete()


class Migration(migrations.Migration):
    # Each batch commits on its own: logging_event is the largest table in most
    # deployments, and one transaction around the whole scan would hold every
    # deleted row locked until the end and throw the work away on interruption.
    # The pass is idempotent, so a restart simply resumes.
    atomic = False

    dependencies = [
        ("marketplace", "0295_offering_merge_progress"),
    ]

    operations = [
        migrations.RunPython(clean_noop_price_logs, migrations.RunPython.noop),
    ]
