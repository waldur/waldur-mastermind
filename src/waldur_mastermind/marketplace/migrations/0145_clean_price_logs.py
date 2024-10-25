from django.db import migrations

from waldur_mastermind.common.utils import prices_are_equal


def clean_price_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    events = Event.objects.filter(
        event_type__in=[
            "marketplace_plan_component_current_price_updated",
            "marketplace_plan_component_future_price_updated",
        ]
    )
    for event in events:
        old_value = event.context["old_value"]
        new_value = event.context["new_value"]
        if prices_are_equal(old_value, new_value):
            event.delete()


class Migration(migrations.Migration):
    dependencies = [
        (
            "marketplace",
            "0144_rename_requested_downscaling_resource_downscaled_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(clean_price_logs),
    ]
