from django.db import migrations


def fill_quota_limits_and_usages(apps, schema_editor):
    Quota = apps.get_model("quotas", "Quota")
    QuotaLimit = apps.get_model("quotas", "QuotaLimit")
    QuotaUsage = apps.get_model("quotas", "QuotaUsage")

    for quota in Quota.objects.all():
        QuotaLimit.objects.create(
            content_type=quota.content_type,
            object_id=quota.object_id,
            name=quota.name,
            value=quota.limit,
        )
        QuotaUsage.objects.create(
            content_type=quota.content_type,
            object_id=quota.object_id,
            name=quota.name,
            delta=quota.usage,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("quotas", "0003_redesign"),
    ]

    operations = [
        migrations.RunPython(fill_quota_limits_and_usages),
        migrations.DeleteModel(
            name="Quota",
        ),
    ]
