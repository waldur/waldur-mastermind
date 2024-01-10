from decimal import Decimal

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
import upload_validator
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators
import waldur_core.logging.loggers
import waldur_core.media.models
import waldur_core.structure.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("structure", "0001_squashed_0036"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Category",
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
                ("title", models.CharField(max_length=255)),
                (
                    "icon",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to="marketplace_category_icons",
                        validators=[
                            upload_validator.FileTypeValidator(
                                allowed_types=[
                                    "image/png",
                                    "image/gif",
                                    "image/jpeg",
                                    "image/svg",
                                    "image/svg+xml",
                                    "image/x-icon",
                                ]
                            )
                        ],
                    ),
                ),
                ("description", models.TextField(blank=True)),
                ("backend_id", models.CharField(blank=True, max_length=255)),
                ("description_en", models.TextField(blank=True, null=True)),
                ("description_et", models.TextField(blank=True, null=True)),
                ("title_en", models.CharField(max_length=255, null=True)),
                ("title_et", models.CharField(max_length=255, null=True)),
                (
                    "default_vm_category",
                    models.BooleanField(
                        default=False,
                        help_text='Set to "true" if this category is for OpenStack VM. Only one category can have "true" value.',
                    ),
                ),
                (
                    "default_volume_category",
                    models.BooleanField(
                        default=False,
                        help_text='Set to true if this category is for OpenStack Volume. Only one category can have "true" value.',
                    ),
                ),
                (
                    "default_tenant_category",
                    models.BooleanField(
                        default=False,
                        help_text='Set to true if this category is for OpenStack Tenant. Only one category can have "true" value.',
                    ),
                ),
            ],
            options={
                "ordering": ("title",),
                "verbose_name": "Category",
                "verbose_name_plural": "Categories",
            },
        ),
        migrations.CreateModel(
            name="CategoryComponent",
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
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="Display name for the measured unit, for example, Floating IP.",
                        max_length=150,
                    ),
                ),
                (
                    "type",
                    models.CharField(
                        help_text="Unique internal name of the measured unit, for example floating_ip.",
                        max_length=50,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-zA-Z0-9_\\-\\/:]+$"
                            )
                        ],
                    ),
                ),
                (
                    "measured_unit",
                    models.CharField(
                        blank=True,
                        help_text="Unit of measurement, for example, GB.",
                        max_length=30,
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="marketplace.category",
                    ),
                ),
            ],
            options={
                "unique_together": {("type", "category")},
            },
        ),
        migrations.CreateModel(
            name="Offering",
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
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name="name",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "thumbnail",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to="marketplace_service_offering_thumbnails",
                        validators=[
                            upload_validator.FileTypeValidator(
                                allowed_types=[
                                    "image/png",
                                    "image/gif",
                                    "image/jpeg",
                                    "image/svg",
                                    "image/svg+xml",
                                    "image/x-icon",
                                ]
                            )
                        ],
                    ),
                ),
                ("full_description", models.TextField(blank=True)),
                ("vendor_details", models.TextField(blank=True)),
                (
                    "rating",
                    models.IntegerField(
                        help_text="Rating is value from 1 to 5.",
                        null=True,
                        validators=[
                            django.core.validators.MaxValueValidator(5),
                            django.core.validators.MinValueValidator(1),
                        ],
                    ),
                ),
                (
                    "attributes",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Fields describing Category.",
                    ),
                ),
                (
                    "options",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Fields describing Offering request form.",
                    ),
                ),
                (
                    "native_name",
                    models.CharField(blank=True, default="", max_length=160),
                ),
                (
                    "native_description",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                ("terms_of_service", models.TextField(blank=True)),
                ("type", models.CharField(max_length=100)),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "Draft"),
                            (2, "Active"),
                            (3, "Paused"),
                            (4, "Archived"),
                        ],
                        default=1,
                    ),
                ),
                ("paused_reason", models.TextField(blank=True)),
                (
                    "shared",
                    models.BooleanField(
                        default=True, help_text="Accessible to all customers."
                    ),
                ),
                (
                    "billable",
                    models.BooleanField(
                        default=True, help_text="Purchase and usage is invoiced."
                    ),
                ),
                ("backend_id", models.CharField(blank=True, max_length=255)),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="offerings",
                        to="marketplace.category",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="structure.customer",
                    ),
                ),
                (
                    "plugin_options",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Public data used by specific plugin, such as storage mode for OpenStack.",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.offering",
                    ),
                ),
                (
                    "datacite_doi",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Datacite DOI"
                    ),
                ),
                (
                    "secret_options",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Private data used by specific plugin, such as credentials and hooks.",
                    ),
                ),
                (
                    "citation_count",
                    models.IntegerField(
                        default=-1, help_text="Number of citations of a DOI"
                    ),
                ),
                ("error_message", models.TextField(blank=True)),
                ("latitude", models.FloatField(blank=True, null=True)),
                ("longitude", models.FloatField(blank=True, null=True)),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="structure.project",
                    ),
                ),
                (
                    "divisions",
                    models.ManyToManyField(
                        blank=True, related_name="offerings", to="structure.Division"
                    ),
                ),
                ("privacy_policy_link", models.URLField(blank=True)),
                ("terms_of_service_link", models.URLField(blank=True)),
                (
                    "image",
                    models.ImageField(
                        blank=True,
                        null=True,
                        upload_to=waldur_core.media.models.get_upload_path,
                    ),
                ),
                (
                    "access_url",
                    models.URLField(
                        blank=True, help_text="URL for accessing management console."
                    ),
                ),
                ("country", models.CharField(blank=True, max_length=2)),
            ],
            options={
                "verbose_name": "Offering",
                "ordering": ["name"],
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name="OfferingComponent",
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
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                ("article_code", models.CharField(blank=True, max_length=30)),
                (
                    "name",
                    models.CharField(
                        help_text="Display name for the measured unit, for example, Floating IP.",
                        max_length=150,
                    ),
                ),
                (
                    "type",
                    models.CharField(
                        help_text="Unique internal name of the measured unit, for example floating_ip.",
                        max_length=50,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-zA-Z0-9_\\-\\/:]+$"
                            )
                        ],
                    ),
                ),
                (
                    "measured_unit",
                    models.CharField(
                        blank=True,
                        help_text="Unit of measurement, for example, GB.",
                        max_length=30,
                    ),
                ),
                (
                    "billing_type",
                    models.CharField(
                        choices=[
                            ("fixed", "Fixed-price"),
                            ("usage", "Usage-based"),
                            ("limit", "Limit-based"),
                            ("one", "One-time"),
                            ("few", "One-time on plan switch"),
                        ],
                        default="fixed",
                        max_length=5,
                    ),
                ),
                (
                    "limit_period",
                    models.CharField(
                        blank=True,
                        choices=[
                            (
                                "month",
                                "Maximum monthly - every month service provider can report up to the amount requested by user.",
                            ),
                            (
                                "annual",
                                "Maximum annually - every year service provider can report up to the amount requested by user.",
                            ),
                            (
                                "total",
                                "Maximum total - SP can report up to the requested amount over the whole active state of resource.",
                            ),
                        ],
                        max_length=6,
                        null=True,
                    ),
                ),
                ("limit_amount", models.IntegerField(blank=True, null=True)),
                ("max_value", models.IntegerField(blank=True, null=True)),
                ("min_value", models.IntegerField(blank=True, null=True)),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="marketplace.offering",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.categorycomponent",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                ("is_boolean", models.BooleanField(default=False)),
                ("default_limit", models.IntegerField(blank=True, null=True)),
                ("backend_id", models.CharField(blank=True, max_length=255)),
                ("max_available_limit", models.IntegerField(blank=True, null=True)),
            ],
            options={
                "ordering": ("name",),
                "unique_together": {("type", "offering")},
            },
        ),
        migrations.CreateModel(
            name="OfferingFile",
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
                (
                    "name",
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name="name",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("file", models.FileField(upload_to="offering_files")),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="files",
                        to="marketplace.offering",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Order",
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
                    "approved_at",
                    models.DateTimeField(blank=True, editable=False, null=True),
                ),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "requested for approval"),
                            (2, "executing"),
                            (3, "done"),
                            (4, "terminated"),
                            (5, "erred"),
                            (6, "rejected"),
                        ],
                        default=1,
                    ),
                ),
                (
                    "total_cost",
                    models.DecimalField(
                        blank=True, decimal_places=10, max_digits=22, null=True
                    ),
                ),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="orders",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="structure.project",
                    ),
                ),
            ],
            options={
                "ordering": ("created",),
                "verbose_name": "Order",
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name="Plan",
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
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name="name",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "unit_price",
                    models.DecimalField(
                        decimal_places=7,
                        default=0,
                        max_digits=22,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0"))
                        ],
                    ),
                ),
                (
                    "unit",
                    models.CharField(
                        choices=[
                            ("month", "Per month"),
                            ("half_month", "Per half month"),
                            ("day", "Per day"),
                            ("hour", "Per hour"),
                            ("quantity", "Quantity"),
                        ],
                        default="day",
                        max_length=30,
                    ),
                ),
                ("article_code", models.CharField(blank=True, max_length=30)),
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "archived",
                    models.BooleanField(
                        default=False, help_text="Forbids creation of new resources."
                    ),
                ),
                ("backend_id", models.CharField(blank=True, max_length=255)),
                (
                    "max_amount",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        help_text="Maximum number of plans that could be active. Plan is disabled when maximum amount is reached.",
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plans",
                        to="marketplace.offering",
                    ),
                ),
                (
                    "divisions",
                    models.ManyToManyField(
                        blank=True, related_name="plans", to="structure.Division"
                    ),
                ),
            ],
            options={
                "ordering": ("name",),
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name="Resource",
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
                (
                    "name",
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name="name",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "cost",
                    models.DecimalField(
                        blank=True, decimal_places=10, max_digits=22, null=True
                    ),
                ),
                ("limits", models.JSONField(blank=True, default=dict)),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "Creating"),
                            (2, "OK"),
                            (3, "Erred"),
                            (4, "Updating"),
                            (5, "Terminating"),
                            (6, "Terminated"),
                        ],
                        default=1,
                    ),
                ),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("backend_metadata", models.JSONField(blank=True, default=dict)),
                ("current_usages", models.JSONField(blank=True, default=dict)),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="marketplace.offering",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.plan",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="structure.project",
                    ),
                ),
                ("backend_id", models.CharField(blank=True, max_length=255)),
                ("report", models.JSONField(blank=True, null=True)),
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                (
                    "end_date",
                    models.DateField(
                        blank=True,
                        help_text="The date is inclusive. Once reached, a resource will be scheduled for termination.",
                        null=True,
                    ),
                ),
                ("effective_id", models.CharField(blank=True, max_length=255)),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="children",
                        to="marketplace.resource",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "ordering": ["created"],
            },
            bases=(waldur_core.structure.models.StructureLoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name="ResourcePlanPeriod",
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
                (
                    "start",
                    models.DateTimeField(blank=True, null=True, verbose_name="start"),
                ),
                (
                    "end",
                    models.DateTimeField(blank=True, null=True, verbose_name="end"),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.plan",
                    ),
                ),
                (
                    "resource",
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
        ),
        migrations.CreateModel(
            name="Section",
            fields=[
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
                    "key",
                    models.CharField(max_length=255, primary_key=True, serialize=False),
                ),
                ("title", models.CharField(max_length=255)),
                (
                    "is_standalone",
                    models.BooleanField(
                        default=False,
                        help_text="Whether section is rendered as a separate tab.",
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sections",
                        to="marketplace.category",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Attribute",
            fields=[
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
                    "key",
                    models.CharField(
                        max_length=255,
                        primary_key=True,
                        serialize=False,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-zA-Z0-9_\\-\\/:]+$"
                            )
                        ],
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                (
                    "type",
                    models.CharField(
                        choices=[
                            ("boolean", "boolean"),
                            ("string", "string"),
                            ("text", "text"),
                            ("integer", "integer"),
                            ("choice", "choice"),
                            ("list", "list"),
                        ],
                        max_length=255,
                    ),
                ),
                (
                    "required",
                    models.BooleanField(
                        default=False,
                        help_text="A value must be provided for the attribute.",
                    ),
                ),
                ("default", models.JSONField(blank=True, null=True)),
                (
                    "section",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attributes",
                        to="marketplace.section",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="AggregateResourceCount",
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
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                ("count", models.PositiveIntegerField(default=0)),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="marketplace.category",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "unique_together": {("category", "content_type", "object_id")},
            },
        ),
        migrations.CreateModel(
            name="CategoryColumn",
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
                    "index",
                    models.PositiveSmallIntegerField(
                        help_text="Index allows to reorder columns."
                    ),
                ),
                (
                    "title",
                    models.CharField(
                        help_text="Title is rendered as column header.", max_length=255
                    ),
                ),
                (
                    "attribute",
                    models.CharField(
                        blank=True,
                        help_text="Resource attribute is rendered as table cell.",
                        max_length=255,
                    ),
                ),
                (
                    "widget",
                    models.CharField(
                        blank=True,
                        help_text="Widget field allows to customise table cell rendering.",
                        max_length=255,
                        null=True,
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="columns",
                        to="marketplace.category",
                    ),
                ),
            ],
            options={
                "ordering": ("category", "index"),
            },
        ),
        migrations.CreateModel(
            name="PlanComponent",
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
                ("amount", models.PositiveIntegerField(default=0)),
                (
                    "price",
                    models.DecimalField(
                        decimal_places=10,
                        default=0,
                        max_digits=22,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0"))
                        ],
                        verbose_name="Price per unit per billing period.",
                    ),
                ),
                (
                    "component",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="marketplace.offeringcomponent",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="marketplace.plan",
                    ),
                ),
            ],
            options={
                "unique_together": {("plan", "component")},
                "ordering": ("component__name",),
            },
        ),
        migrations.CreateModel(
            name="CategoryComponentUsage",
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
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                ("date", models.DateField()),
                ("reported_usage", models.BigIntegerField(null=True)),
                ("fixed_usage", models.BigIntegerField(null=True)),
                (
                    "component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.categorycomponent",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Screenshot",
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
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name="name",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "image",
                    models.ImageField(
                        upload_to=waldur_core.media.models.get_upload_path
                    ),
                ),
                (
                    "thumbnail",
                    models.ImageField(
                        editable=False,
                        null=True,
                        upload_to=waldur_core.media.models.get_upload_path,
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="screenshots",
                        to="marketplace.offering",
                    ),
                ),
            ],
            options={
                "verbose_name": "Screenshot",
            },
        ),
        migrations.CreateModel(
            name="ComponentUsage",
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
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("usage", models.BigIntegerField(default=0)),
                ("date", models.DateTimeField()),
                ("billing_period", models.DateField()),
                (
                    "component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.offeringcomponent",
                    ),
                ),
                (
                    "plan_period",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="marketplace.resourceplanperiod",
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="usages",
                        to="marketplace.resource",
                    ),
                ),
                (
                    "recurring",
                    models.BooleanField(
                        default=False,
                        help_text="Reported value is reused every month until changed.",
                    ),
                ),
                ("backend_id", models.CharField(blank=True, max_length=255)),
            ],
            options={
                "unique_together": {
                    ("resource", "component", "plan_period", "billing_period")
                },
            },
        ),
        migrations.CreateModel(
            name="ServiceProvider",
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
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=2000, verbose_name="description"
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("enable_notifications", models.BooleanField(default=True)),
                (
                    "api_secret_code",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "lead_email",
                    models.EmailField(
                        blank=True,
                        help_text="Email for notification about new request based orders. If this field is set, notifications will be sent.",
                        max_length=254,
                        null=True,
                    ),
                ),
                (
                    "lead_subject",
                    models.CharField(
                        blank=True,
                        help_text="Notification subject template. Django template variables can be used.",
                        max_length=255,
                    ),
                ),
                (
                    "lead_body",
                    models.TextField(
                        blank=True,
                        help_text="Notification body template. Django template variables can be used.",
                        validators=[
                            waldur_core.core.validators.validate_template_syntax
                        ],
                    ),
                ),
                (
                    "customer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="structure.customer",
                    ),
                ),
                (
                    "image",
                    models.ImageField(
                        blank=True,
                        null=True,
                        upload_to=waldur_core.media.models.get_upload_path,
                    ),
                ),
            ],
            options={
                "verbose_name": "Service provider",
            },
        ),
        migrations.CreateModel(
            name="ComponentQuota",
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
                ("limit", models.BigIntegerField(default=-1)),
                ("usage", models.BigIntegerField(default=0)),
                (
                    "component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.offeringcomponent",
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="quotas",
                        to="marketplace.resource",
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
            ],
            options={
                "unique_together": {("resource", "component")},
            },
        ),
        migrations.CreateModel(
            name="CartItem",
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
                    "cost",
                    models.DecimalField(
                        blank=True, decimal_places=10, max_digits=22, null=True
                    ),
                ),
                ("limits", models.JSONField(blank=True, default=dict)),
                (
                    "type",
                    models.PositiveSmallIntegerField(
                        choices=[(1, "Create"), (2, "Update"), (3, "Terminate")],
                        default=1,
                    ),
                ),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="marketplace.offering",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.plan",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="structure.project",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("created",),
            },
        ),
        migrations.CreateModel(
            name="OrderItem",
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
                (
                    "cost",
                    models.DecimalField(
                        blank=True, decimal_places=10, max_digits=22, null=True
                    ),
                ),
                ("limits", models.JSONField(blank=True, default=dict)),
                (
                    "type",
                    models.PositiveSmallIntegerField(
                        choices=[(1, "Create"), (2, "Update"), (3, "Terminate")],
                        default=1,
                    ),
                ),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "pending"),
                            (2, "executing"),
                            (3, "done"),
                            (4, "erred"),
                            (5, "terminated"),
                            (6, "terminating"),
                        ],
                        default=1,
                    ),
                ),
                (
                    "activated",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="activation date"
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.offering",
                        related_name="+",
                    ),
                ),
                (
                    "old_plan",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="marketplace.plan",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="marketplace.order",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.plan",
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.resource",
                    ),
                ),
                ("output", models.TextField(blank=True)),
                ("error_traceback", models.TextField(blank=True)),
                ("backend_id", models.CharField(blank=True, max_length=255)),
                (
                    "reviewed_at",
                    models.DateTimeField(blank=True, editable=False, null=True),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("callback_url", models.URLField(blank=True, null=True)),
            ],
            options={
                "ordering": ("created",),
                "verbose_name": "Order",
            },
            bases=(waldur_core.structure.models.StructureLoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name="OfferingPermission",
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
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("expiration_time", models.DateTimeField(blank=True, null=True)),
                (
                    "is_active",
                    models.BooleanField(db_index=True, default=True, null=True),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="permissions",
                        to="marketplace.offering",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "unique_together": {("offering", "user", "is_active")},
            },
        ),
        migrations.CreateModel(
            name="OfferingUser",
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
                ("username", models.CharField(blank=True, max_length=100, null=True)),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.offering",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
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
            ],
            options={
                "ordering": ["username"],
                "unique_together": {("offering", "user")},
            },
        ),
        migrations.CreateModel(
            name="AttributeOption",
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
                    "key",
                    models.CharField(
                        max_length=255,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-zA-Z0-9_\\-\\/:]+$"
                            )
                        ],
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                (
                    "attribute",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="options",
                        to="marketplace.attribute",
                    ),
                ),
            ],
            options={
                "unique_together": {("attribute", "key")},
            },
        ),
        migrations.CreateModel(
            name="CategoryHelpArticle",
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
                ("title", models.CharField(blank=True, max_length=255, null=True)),
                ("url", models.URLField()),
                (
                    "categories",
                    models.ManyToManyField(
                        blank=True, related_name="articles", to="marketplace.Category"
                    ),
                ),
            ],
        ),
    ]
