from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0054_tokenscopeduser"),
    ]

    operations = [
        migrations.CreateModel(
            name="VersionUpdateNotification",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("version", models.CharField(max_length=100)),
                ("recipient", models.EmailField(max_length=254)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-sent_at", "id"],
                "unique_together": {("version", "recipient")},
            },
        ),
    ]
