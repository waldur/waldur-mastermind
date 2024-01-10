from django.db import migrations
from django.db.models import Count


def clean_componentusage(apps, schema_editor):
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    resource_ids: list[str] = (
        ComponentUsage.objects.filter(plan_period=None)
        .values("resource_id", "component_id", "billing_period")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .values_list("resource_id", flat=True)
        .distinct()
    )
    for resource_id in resource_ids:
        pairs: list[dict] = (
            ComponentUsage.objects.filter(plan_period=None, resource_id=resource_id)
            .values("component_id", "billing_period")
            .annotate(count=Count("id"))
            .filter(count__gt=1)
            .values("component_id", "billing_period")
        )
        for pair in pairs:
            filters = dict(
                plan_period=None,
                resource_id=resource_id,
                component_id=pair["component_id"],
                billing_period=pair["billing_period"],
            )
            max_usage = (
                ComponentUsage.objects.filter(**filters).order_by("usage").last()
            )
            ComponentUsage.objects.filter(**filters).exclude(id=max_usage.id).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0118_resource_options"),
    ]

    operations = [
        migrations.RunPython(clean_componentusage),
    ]
