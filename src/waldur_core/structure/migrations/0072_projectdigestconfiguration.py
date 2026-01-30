import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("structure", "0071_customer_user_assurance_levels_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectDigestConfiguration",
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
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("is_enabled", models.BooleanField(default=False)),
                (
                    "frequency",
                    models.CharField(
                        choices=[
                            ("weekly", "Weekly"),
                            ("biweekly", "Bi-weekly"),
                            ("monthly", "Monthly"),
                        ],
                        default="monthly",
                        max_length=20,
                    ),
                ),
                (
                    "enabled_sections",
                    waldur_core.core.fields.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of section keys to include. Empty means all.",
                    ),
                ),
                (
                    "last_sent_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "day_of_week",
                    models.PositiveSmallIntegerField(
                        default=1,
                        help_text="For weekly/biweekly: 0=Sunday..6=Saturday",
                        validators=[
                            django.core.validators.MaxValueValidator(6),
                        ],
                    ),
                ),
                (
                    "day_of_month",
                    models.PositiveSmallIntegerField(
                        default=1,
                        help_text="For monthly: day of month (1-28)",
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(28),
                        ],
                    ),
                ),
                (
                    "customer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_digest_config",
                        to="structure.customer",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Project digest configuration",
                "verbose_name_plural": "Project digest configurations",
            },
        ),
    ]
