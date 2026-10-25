from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0035_consumer_api_key_permission"),
    ]

    operations = [
        migrations.CreateModel(
            name="GlobalRoleScope",
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
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "singleton",
                    models.BooleanField(default=True, editable=False, unique=True),
                ),
            ],
            options={
                "verbose_name": "Global role scope",
            },
        ),
    ]
