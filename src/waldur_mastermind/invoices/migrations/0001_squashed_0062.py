from decimal import Decimal

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators
import waldur_mastermind.invoices.models
import waldur_mastermind.invoices.utils


class Migration(migrations.Migration):
    replaces = [
        ('invoices', '0001_squashed_0030'),
        ('invoices', '0031_rename_invoice_item_model'),
        ('invoices', '0032_genericinvoiceitem_name'),
        ('invoices', '0033_downtime_offering_and_resource'),
        ('invoices', '0034_paymentprofile'),
        ('invoices', '0035_payment_profile_is_active'),
        ('invoices', '0036_paymentprofile_name'),
        ('invoices', '0037_paymentprofile_is_active_null '),
        ('invoices', '0038_payment'),
        ('invoices', '0039_payment_invoice'),
        ('invoices', '0040_invoice_created'),
        ('invoices', '0041_update_invoice_items_scope'),
        ('invoices', '0042_update_invoice_items_resource_name'),
        ('invoices', '0043_drop_package_column'),
        ('invoices', '0044_invoiceitem_resource'),
        ('invoices', '0045_invoiceitem_resource_fix'),
        ('invoices', '0046_invoiceitem_measured_unit'),
        ('invoices', '0047_migrate_slurm_measured_unit'),
        ('invoices', '0048_fix_slurm_invoice_items'),
        ('invoices', '0049_remove_invoice_file_field'),
        ('invoices', '0050_fix_slurm_invoice_items_for_march'),
        ('invoices', '0051_remove_invoiceitem_product_code'),
        ('invoices', '0052_delete_servicedowntime'),
        ('invoices', '0053_invoiceitem_uuid'),
        ('invoices', '0054_fix_resource_limit_periods'),
        ('invoices', '0055_invoice_backend_id'),
        ('invoices', '0056_fill_quantity'),
        ('invoices', '0057_long_project_name'),
        ('invoices', '0058_add_invoice_payment_fields'),
        ('invoices', '0059_json_field'),
        ('invoices', '0060_alter_paymentprofile_is_active'),
        ('invoices', '0061_total_cost'),
        ('invoices', '0062_alter_invoiceitem_resource'),
    ]

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('marketplace', '0073_update_internal_name_validator'),
        ('structure', '0010_customer_geolocation'),
    ]

    operations = [
        migrations.CreateModel(
            name='Invoice',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'month',
                    models.PositiveSmallIntegerField(
                        default=waldur_mastermind.invoices.utils.get_current_month,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(12),
                        ],
                    ),
                ),
                (
                    'year',
                    models.PositiveSmallIntegerField(
                        default=waldur_mastermind.invoices.utils.get_current_year
                    ),
                ),
                (
                    'state',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('created', 'Created'),
                            ('paid', 'Paid'),
                            ('canceled', 'Canceled'),
                        ],
                        default='pending',
                        max_length=30,
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.customer',
                        verbose_name='organization',
                    ),
                ),
                (
                    'invoice_date',
                    models.DateField(
                        blank=True,
                        help_text='Date then invoice moved from state pending to created.',
                        null=True,
                    ),
                ),
                (
                    'tax_percent',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=4,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                (
                    'total_cost',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        editable=False,
                        help_text='Cached value for total cost.',
                        max_digits=10,
                    ),
                ),
                (
                    'created',
                    models.DateField(
                        blank=True,
                        default=waldur_mastermind.invoices.models.get_created_date,
                        null=True,
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'payment_url',
                    models.URLField(
                        blank=True,
                        help_text='URL for initiating payment via payment gateway.',
                    ),
                ),
                (
                    'reference_number',
                    models.CharField(
                        blank=True,
                        help_text='Reference number associated with the invoice.',
                        max_length=300,
                    ),
                ),
            ],
            options={
                'unique_together': {('customer', 'month', 'year')},
            },
        ),
        migrations.CreateModel(
            name='PaymentProfile',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'payment_type',
                    waldur_mastermind.invoices.models.PaymentType(
                        choices=[
                            ('fixed_price', 'Fixed-price contract'),
                            ('invoices', 'Monthly invoices'),
                            ('payment_gw_monthly', ' Payment gateways (monthly)'),
                        ],
                        max_length=30,
                    ),
                ),
                ('attributes', models.JSONField(blank=True, default=dict)),
                (
                    'organization',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to='structure.customer',
                    ),
                ),
                ('is_active', models.BooleanField(default=True, null=True)),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
            ],
            options={
                'unique_together': {('organization', 'is_active')},
            },
        ),
        migrations.CreateModel(
            name='Payment',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'sum',
                    models.DecimalField(decimal_places=2, default=0, max_digits=10),
                ),
                ('date_of_payment', models.DateField()),
                (
                    'profile',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to='invoices.paymentprofile',
                    ),
                ),
                (
                    'proof',
                    models.FileField(
                        blank=True, null=True, upload_to='proof_of_payment'
                    ),
                ),
                (
                    'invoice',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='invoices.invoice',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='InvoiceItem',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'unit_price',
                    models.DecimalField(
                        decimal_places=7,
                        default=0,
                        max_digits=22,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal('0'))
                        ],
                    ),
                ),
                (
                    'unit',
                    models.CharField(
                        choices=[
                            ('month', 'Per month'),
                            ('half_month', 'Per half month'),
                            ('day', 'Per day'),
                            ('hour', 'Per hour'),
                            ('quantity', 'Quantity'),
                        ],
                        default='day',
                        max_length=30,
                    ),
                ),
                ('article_code', models.CharField(blank=True, max_length=30)),
                (
                    'start',
                    models.DateTimeField(
                        default=waldur_mastermind.invoices.utils.get_current_month_start,
                        help_text='Date and time when item usage has started.',
                    ),
                ),
                (
                    'end',
                    models.DateTimeField(
                        default=waldur_mastermind.invoices.utils.get_current_month_end,
                        help_text='Date and time when item usage has ended.',
                    ),
                ),
                ('project_name', models.CharField(blank=True, max_length=500)),
                ('project_uuid', models.CharField(blank=True, max_length=32)),
                (
                    'details',
                    models.JSONField(
                        blank=True, default=dict, help_text='Stores data about scope'
                    ),
                ),
                (
                    'invoice',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='items',
                        to='invoices.invoice',
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='structure.project',
                    ),
                ),
                (
                    'quantity',
                    models.DecimalField(decimal_places=7, default=0, max_digits=22),
                ),
                ('name', models.TextField(default='')),
                (
                    'resource',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='invoice_items',
                        to='marketplace.resource',
                    ),
                ),
                (
                    'measured_unit',
                    models.CharField(
                        blank=True,
                        help_text='Unit of measurement, for example, GB.',
                        max_length=30,
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
