from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0219_courseaccount_pending_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="offering",
            name="helpdesk_url",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="offering",
            name="documentation_url",
            field=models.URLField(blank=True),
        ),
    ]
