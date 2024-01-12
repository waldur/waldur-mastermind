from django.db import migrations


class States:
    CREATING = 1
    OK = 2
    ERRED = 3
    UPDATING = 4
    TERMINATING = 5
    TERMINATED = 6


def add_missing_plan_periods(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")
    ResourcePlanPeriod = apps.get_model("marketplace", "ResourcePlanPeriod")
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    for resource in Resource.objects.filter(
        state__in=(States.OK, States.UPDATING)
    ).exclude(plan=None):
        if ResourcePlanPeriod.objects.filter(resource=resource, end=None).exists():
            continue
        plan_period = ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=resource.created,
            end=None,
        )
        ComponentUsage.objects.filter(resource=resource, plan_period=None).update(
            plan_period=plan_period
        )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0121_clean_invalid_usage"),
    ]

    operations = [
        migrations.RunPython(add_missing_plan_periods),
    ]
