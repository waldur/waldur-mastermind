from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("media", "0003_mimetype"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="file",
            name="is_public",
        ),
        migrations.AddField(
            model_name="file",
            name="hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
