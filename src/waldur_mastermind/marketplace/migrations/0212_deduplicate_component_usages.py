from django.db import migrations
from django.db.models import Count


def deduplicate_component_usages(apps, schema_editor):
    """Remove duplicate ComponentUsage records that share the same
    (resource, component, billing_period) but differ in plan_period.

    Keeps the record with the highest usage value and re-parents any
    ComponentUserUsage records to the surviving record before deleting
    the duplicates.
    """
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    ComponentUserUsage = apps.get_model("marketplace", "ComponentUserUsage")

    duplicates = (
        ComponentUsage.objects.values("resource_id", "component_id", "billing_period")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
    )

    for dup in duplicates:
        qs = ComponentUsage.objects.filter(
            resource_id=dup["resource_id"],
            component_id=dup["component_id"],
            billing_period=dup["billing_period"],
        )
        # Keep the record with the highest usage
        keeper = qs.order_by("-usage").first()
        duplicates_to_remove = qs.exclude(id=keeper.id)

        # Re-parent any ComponentUserUsage records to the keeper.
        # First, drop records that would violate the (username, component_usage)
        # unique constraint because the keeper already has an entry for that user.
        keeper_usernames = ComponentUserUsage.objects.filter(
            component_usage=keeper
        ).values_list("username", flat=True)
        ComponentUserUsage.objects.filter(
            component_usage__in=duplicates_to_remove,
            username__in=keeper_usernames,
        ).delete()
        ComponentUserUsage.objects.filter(
            component_usage__in=duplicates_to_remove
        ).update(component_usage=keeper)

        duplicates_to_remove.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0211_offeringuserattributeconfig_expose_active_isds_and_more"),
    ]

    operations = [
        migrations.RunPython(deduplicate_component_usages),
    ]
