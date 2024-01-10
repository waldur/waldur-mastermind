from django.db import migrations, models


class States:
    PENDING_CONSUMER = 1
    PENDING_PROVIDER = 7
    EXECUTING = 2
    DONE = 3
    ERRED = 4
    CANCELED = 5
    REJECTED = 6


def init_consumer_reviewed_by(apps, schema_editor):
    Order = apps.get_model("marketplace", "Order")
    Order.objects.filter(
        consumer_reviewed_by__isnull=True,
        consumer_reviewed_at__isnull=True,
        state__in=(States.PENDING_PROVIDER, States.EXECUTING, States.DONE),
    ).update(
        consumer_reviewed_by=models.F("created_by"),
        consumer_reviewed_at=models.F("created"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0114_alter_order_resource"),
    ]

    operations = [
        migrations.RunPython(init_consumer_reviewed_by),
    ]
