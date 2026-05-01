import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("permissions", "0020_set_consumer_info_permission"),
    ]

    operations = [
        migrations.AlterField(
            model_name="role",
            name="name",
            field=models.CharField(db_index=True, max_length=150),
        ),
        migrations.CreateModel(
            name="RoleAvailability",
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
                ("object_id", models.PositiveIntegerField()),
                (
                    "content_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="availability",
                        to="permissions.role",
                    ),
                ),
            ],
            options={
                "unique_together": {("role", "content_type", "object_id")},
            },
        ),
    ]
