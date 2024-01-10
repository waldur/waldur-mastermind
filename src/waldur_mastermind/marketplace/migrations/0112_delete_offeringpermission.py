from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0111_consumer_reviewed_by"),
        ("permissions", "0002_import_data"),
    ]

    operations = [
        migrations.DeleteModel(
            name="OfferingPermission",
        ),
    ]
