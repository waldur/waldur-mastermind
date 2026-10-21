import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0308_spending_limits"),
    ]

    operations = [
        migrations.RenameField(
            model_name="resourceapikey",
            old_name="limits",
            new_name="spending_limits",
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="spending_limits",
            field=models.JSONField(
                blank=True,
                help_text="Limits per component type and period, as {component_type: {period: amount}}. Waldur pauses the key once its usage in a period reaches that period's limit.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="paused_by_limit",
            field=models.BooleanField(
                default=False,
                help_text="Waldur paused the key because its usage reached a limit. Such a key is resumed automatically once its usage is under its limits again: when the period of the limit resets, or when a limit is raised.",
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="limit_resumes_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When every limit that paused the key has reset, so that it resumes without a new usage report.",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="ResourceApiKeyDailyUsage",
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
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("component_type", models.CharField(max_length=50)),
                ("date", models.DateField()),
                (
                    "usage",
                    models.DecimalField(decimal_places=2, default=0, max_digits=20),
                ),
                (
                    "api_key",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="daily_usages",
                        to="marketplace.resourceapikey",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("api_key", "component_type", "date"),
                        name="mp_resource_api_key_daily_usage_unique",
                    )
                ],
            },
        ),
    ]
