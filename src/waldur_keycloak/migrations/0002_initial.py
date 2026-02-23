import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators


class CreateModelIfTableNotExists(migrations.CreateModel):
    """CreateModel variant that skips DB table creation when the table already exists.

    This handles the case where tables were created by a prior deployment
    but the migration was not recorded in django_migrations.
    Django state is always updated so the migration graph stays consistent.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        table_name = model._meta.db_table
        if table_name in schema_editor.connection.introspection.table_names():
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("waldur_keycloak", "0001_initial"),
        ("marketplace", "0208_order_rejection_comments"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        CreateModelIfTableNotExists(
            name="OfferingKeycloakGroup",
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
                ("backend_id", models.CharField(blank=True, max_length=255)),
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
                (
                    "name",
                    models.CharField(
                        blank=True, max_length=150, verbose_name="Group name"
                    ),
                ),
                (
                    "scope_id",
                    models.UUIDField(
                        blank=True,
                        help_text="Sub-entity UUID within a resource, e.g. Rancher Project UUID within a Cluster.",
                        null=True,
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="keycloak_groups",
                        to="marketplace.offering",
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="keycloak_groups",
                        to="marketplace.resource",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="keycloak_groups",
                        to="marketplace.offeringuserrole",
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
                "unique_together": {("offering", "role", "resource", "scope_id")},
            },
        ),
        CreateModelIfTableNotExists(
            name="OfferingKeycloakMembership",
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
                    "error_message",
                    models.TextField(blank=True),
                ),
                (
                    "error_traceback",
                    models.TextField(blank=True),
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
                (
                    "username",
                    models.CharField(
                        help_text="Keycloak user username", max_length=255
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        help_text="User's email for notifications",
                        max_length=254,
                    ),
                ),
                (
                    "first_name",
                    models.CharField(blank=True, max_length=100),
                ),
                (
                    "last_name",
                    models.CharField(blank=True, max_length=100),
                ),
                (
                    "state",
                    django_fsm.FSMField(
                        choices=[
                            ("pending", "pending"),
                            ("active", "active"),
                        ],
                        default="pending",
                        max_length=50,
                    ),
                ),
                ("last_checked", models.DateTimeField(auto_now=True)),
                (
                    "group",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="waldur_keycloak.offeringkeycloakgroup",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
                "unique_together": {("username", "group")},
            },
        ),
    ]
