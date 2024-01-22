from django.db import migrations


def drop_druplicate_usage(apps, schema_editor):
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    ComponentUsage.objects.filter(plan_period=None).exclude(
        resource__plan=None
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0122_add_missing_plan_periods"),
    ]

    operations = [
        migrations.RunPython(drop_druplicate_usage),
    ]
