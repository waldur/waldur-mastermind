from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0049_dedup_notificationtemplate_paths"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationtemplate",
            name="path",
            field=models.CharField(
                help_text="Example: 'flatpages/default.html'",
                max_length=150,
                unique=True,
                verbose_name="path",
            ),
        ),
    ]
