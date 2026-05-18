import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0236_project_order_auto_approval"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ResourceLimitChangeRequest",
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
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "draft"),
                            (2, "pending"),
                            (3, "approved"),
                            (4, "rejected"),
                            (5, "canceled"),
                        ],
                        default=1,
                    ),
                ),
                (
                    "reviewed_at",
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        help_text="Timestamp when the review was completed",
                        null=True,
                    ),
                ),
                (
                    "review_comment",
                    models.TextField(
                        blank=True,
                        help_text="Optional comment provided during review",
                        null=True,
                    ),
                ),
                (
                    "requested_limits",
                    models.JSONField(),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="limit_change_requests",
                        to="marketplace.resource",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="User who performed the review",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Resource limit change request",
                "verbose_name_plural": "Resource limit change requests",
                "ordering": ["created"],
            },
        ),
        migrations.AddConstraint(
            model_name="resourcelimitchangerequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(state=2),  # ReviewStates.PENDING
                fields=("resource", "created_by"),
                name="unique_pending_limit_change_request_per_resource_and_user",
            ),
        ),
    ]
