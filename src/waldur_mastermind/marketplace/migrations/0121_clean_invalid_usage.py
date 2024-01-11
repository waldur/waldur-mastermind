from django.db import migrations
from django.db.models import F


PLUGIN_NAME = "Waldur.RemoteOffering"


def clean_componentusage(apps, schema_editor):
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    ComponentUsage.objects.filter(
        resource__offering__type=PLUGIN_NAME, resource__backend_id=""
    ).delete()
    ComponentUsage.objects.filter(
        resource__offering__type=PLUGIN_NAME, date__lt=F('resource__created')
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0120_componentusage_unique_together"),
    ]

    operations = [
        migrations.RunPython(clean_componentusage),
    ]
