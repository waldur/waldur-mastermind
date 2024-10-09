# Generated by Django 4.2.14 on 2024-10-09 16:40

import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("marketplace", "0141_resource_restrict_member_access"),
    ]

    operations = [
        migrations.CreateModel(
            name="Migration",
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
                ("error_message", models.TextField(blank=True)),
                ("error_traceback", models.TextField(blank=True)),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, "Creation Scheduled"),
                            (6, "Creating"),
                            (1, "Update Scheduled"),
                            (2, "Updating"),
                            (7, "Deletion Scheduled"),
                            (8, "Deleting"),
                            (3, "OK"),
                            (4, "Erred"),
                        ],
                        default=5,
                    ),
                ),
                ("mappings", waldur_core.core.fields.JSONField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "dst_resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="marketplace.resource",
                    ),
                ),
                (
                    "src_resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="marketplace.resource",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
            bases=(models.Model, django_fsm.ConcurrentTransitionMixin),
        ),
    ]
