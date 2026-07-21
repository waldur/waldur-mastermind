import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.logging.models


class Migration(migrations.Migration):
    """Create EventConsumer and its scope bindings.

    A consumer is bound to a *list* of entities via EventConsumerScope
    (PAT-style), so it can watch several projects, a customer, an offering, or
    a mix. An EventConsumer with NO bindings is a GLOBAL consumer.
    """

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("logging", "0024_alter_webhook_destination_url"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EventConsumer",
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
                    "rmq_username",
                    models.CharField(
                        blank=True,
                        help_text="RabbitMQ username (UUID hex) for the consumer queue.",
                        max_length=32,
                    ),
                ),
                ("queue_created", models.BooleanField(default=False)),
                (
                    "object_types",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of observable object types this consumer receives. Empty list means all types.",
                        validators=[
                            waldur_core.logging.models.validate_observable_object_types
                        ],
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        help_text="Owner/registrant; the RabbitMQ vhost is the user UUID hex.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="event_consumers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EventConsumerScope",
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
                ("object_id", models.PositiveIntegerField(db_index=True)),
                (
                    "consumer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="scopes",
                        to="logging.eventconsumer",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["content_type", "object_id"],
                        name="logging_eve_content_1ebe33_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("consumer", "content_type", "object_id"),
                        name="unique_event_consumer_scope",
                    )
                ],
            },
        ),
    ]
