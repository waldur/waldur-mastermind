from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("media", "0005_fill_hash"),
    ]

    operations = [
        migrations.AlterField(
            model_name="file",
            name="hash",
            field=models.CharField(
                max_length=64, blank=False, null=False, db_index=True
            ),
        ),
    ]
