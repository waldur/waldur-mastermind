from django.db import migrations


def drop_zero_usage(apps, schema_editor):
    QuotaUsage = apps.get_model("quotas", "QuotaUsage")
    QuotaUsage.objects.filter(delta=0).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("quotas", "0004_delete_quota"),
    ]

    operations = [
        migrations.RunPython(drop_zero_usage),
    ]
