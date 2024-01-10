from django.db import migrations


def fill_order_item(apps, schema_editor):
    DryRun = apps.get_model("marketplace_script", "DryRun")
    for row in DryRun.objects.all():
        row.order_item = row.order.items.first()
        row.save(update_fields=["order_item"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_script", "0002_dryrun_order_item"),
    ]

    operations = [
        migrations.RunPython(fill_order_item),
    ]
