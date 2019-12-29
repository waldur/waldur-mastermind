# -*- coding: utf-8 -*-
from decimal import Decimal
from django.conf import settings
import django.contrib.postgres.fields.jsonb
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
import waldur_core.core.fields
import waldur_core.core.validators
import waldur_core.logging.loggers
import waldur_core.media.models
import waldur_core.media.validators
import waldur_core.structure.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('structure', '0009_project_is_removed'),
        ('contenttypes', '0002_remove_content_type_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AggregateResourceCount',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField(blank=True, null=True)),
                ('count', models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name='Attribute',
            fields=[
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('key', models.CharField(max_length=255, primary_key=True, serialize=False, validators=[django.core.validators.RegexValidator('^[a-zA-Z0-9_]+$')])),
                ('title', models.CharField(max_length=255)),
                ('type', models.CharField(choices=[('boolean', 'boolean'), ('string', 'string'), ('text', 'text'), ('integer', 'integer'), ('choice', 'choice'), ('list', 'list')], max_length=255)),
                ('required', models.BooleanField(default=False, help_text='A value must be provided for the attribute.')),
                ('default', django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='AttributeOption',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=255, validators=[django.core.validators.RegexValidator('^[a-zA-Z0-9_]+$')])),
                ('title', models.CharField(max_length=255)),
                ('attribute', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='options', to='marketplace.Attribute')),
            ],
        ),
        migrations.CreateModel(
            name='CartItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('cost', models.DecimalField(blank=True, decimal_places=10, max_digits=22, null=True)),
                ('limits', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('type', models.PositiveSmallIntegerField(choices=[(1, 'Create'), (2, 'Update'), (3, 'Terminate')], default=1)),
                ('attributes', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
            ],
            options={
                'ordering': ('created',),
            },
        ),
        migrations.CreateModel(
            name='Category',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('title', models.CharField(max_length=255)),
                ('icon', models.FileField(blank=True, null=True, upload_to='marketplace_category_icons', validators=[waldur_core.media.validators.FileTypeValidator(allowed_types=['image/png', 'image/gif', 'image/jpeg', 'image/svg', 'image/svg+xml', 'image/x-icon'])])),
                ('description', models.TextField(blank=True)),
                ('backend_id', models.CharField(blank=True, max_length=255)),
            ],
            options={
                'ordering': ('title',),
                'verbose_name': 'Category',
                'verbose_name_plural': 'Categories',
            },
        ),
        migrations.CreateModel(
            name='CategoryColumn',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('index', models.PositiveSmallIntegerField(help_text='Index allows to reorder columns.')),
                ('title', models.CharField(help_text='Title is rendered as column header.', max_length=255)),
                ('attribute', models.CharField(blank=True, help_text='Resource attribute is rendered as table cell.', max_length=255)),
                ('widget', models.CharField(blank=True, help_text='Widget field allows to customise table cell rendering.', max_length=255)),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='columns', to='marketplace.Category')),
            ],
            options={
                'ordering': ('category', 'index'),
            },
        ),
        migrations.CreateModel(
            name='CategoryComponent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(help_text='Display name for the measured unit, for example, Floating IP.', max_length=150)),
                ('type', models.CharField(help_text='Unique internal name of the measured unit, for example floating_ip.', max_length=50, validators=[django.core.validators.RegexValidator('^[a-zA-Z0-9_]+$')])),
                ('measured_unit', models.CharField(blank=True, help_text='Unit of measurement, for example, GB.', max_length=30)),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='components', to='marketplace.Category')),
            ],
        ),
        migrations.CreateModel(
            name='CategoryComponentUsage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField(null=True, blank=True)),
                ('date', models.DateField()),
                ('reported_usage', models.PositiveIntegerField(null=True)),
                ('fixed_usage', models.PositiveIntegerField(null=True)),
                ('component', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='marketplace.CategoryComponent')),
                ('content_type', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ComponentQuota',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('limit', models.PositiveIntegerField(default=-1)),
                ('usage', models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name='ComponentUsage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('usage', models.PositiveIntegerField(default=0)),
                ('date', models.DateTimeField()),
                ('billing_period', models.DateField()),
            ],
        ),
        migrations.CreateModel(
            name='Offering',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('object_id', models.PositiveIntegerField(null=True, blank=True)),
                ('thumbnail', models.FileField(blank=True, null=True, upload_to='marketplace_service_offering_thumbnails', validators=[waldur_core.media.validators.FileTypeValidator(allowed_types=['image/png', 'image/gif', 'image/jpeg', 'image/svg', 'image/svg+xml', 'image/x-icon'])])),
                ('full_description', models.TextField(blank=True)),
                ('vendor_details', models.TextField(blank=True)),
                ('rating', models.IntegerField(help_text='Rating is value from 1 to 5.', null=True, validators=[django.core.validators.MaxValueValidator(5), django.core.validators.MinValueValidator(1)])),
                ('attributes', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict, help_text='Fields describing Category.')),
                ('options', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict, help_text='Fields describing Offering request form.')),
                ('geolocations', waldur_core.core.fields.JSONField(blank=True, default=list, help_text='List of latitudes and longitudes. For example: [{"latitude": 123, "longitude": 345}, {"latitude": 456, "longitude": 678}]')),
                ('native_name', models.CharField(blank=True, default='', max_length=160)),
                ('native_description', models.CharField(blank=True, default='', max_length=500)),
                ('terms_of_service', models.TextField(blank=True)),
                ('type', models.CharField(max_length=100)),
                ('state', django_fsm.FSMIntegerField(choices=[(1, 'Draft'), (2, 'Active'), (3, 'Paused'), (4, 'Archived')], default=1)),
                ('paused_reason', models.TextField(blank=True)),
                ('shared', models.BooleanField(default=True, help_text='Accessible to all customers.')),
                ('billable', models.BooleanField(default=True, help_text='Purchase and usage is invoiced.')),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('allowed_customers', models.ManyToManyField(blank=True, to='structure.Customer')),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='offerings', to='marketplace.Category')),
                ('content_type', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
                ('customer', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='structure.Customer')),
            ],
            options={
                'verbose_name': 'Offering',
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name='OfferingComponent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('product_code', models.CharField(blank=True, max_length=30)),
                ('article_code', models.CharField(blank=True, max_length=30)),
                ('name', models.CharField(help_text='Display name for the measured unit, for example, Floating IP.', max_length=150)),
                ('type', models.CharField(help_text='Unique internal name of the measured unit, for example floating_ip.', max_length=50, validators=[django.core.validators.RegexValidator('^[a-zA-Z0-9_]+$')])),
                ('measured_unit', models.CharField(blank=True, help_text='Unit of measurement, for example, GB.', max_length=30)),
                ('billing_type', models.CharField(choices=[('fixed', 'Fixed-price'), ('usage', 'Usage-based'), ('one', 'One-time'), ('few', 'One-time on plan switch')], default='fixed', max_length=5)),
                ('limit_period', models.CharField(blank=True, choices=[('month', 'Maximum monthly - every month service provider can report up to the amount requested by user.'), ('total', 'Maximum total - SP can report up to the requested amount over the whole active state of resource.')], max_length=5, null=True)),
                ('limit_amount', models.IntegerField(blank=True, null=True)),
                ('max_value', models.IntegerField(blank=True, null=True)),
                ('min_value', models.IntegerField(blank=True, null=True)),
                ('disable_quotas', models.BooleanField(default=False, help_text='Do not allow user to specify quotas when offering is provisioned.')),
                ('offering', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='components', to='marketplace.Offering')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.CategoryComponent')),
            ],
        ),
        migrations.CreateModel(
            name='OfferingFile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('file', models.FileField(upload_to='offering_files')),
                ('offering', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='files', to='marketplace.Offering')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('approved_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('state', django_fsm.FSMIntegerField(choices=[(1, 'requested for approval'), (2, 'executing'), (3, 'done'), (4, 'terminated'), (5, 'erred'), (6, 'rejected')], default=1)),
                ('total_cost', models.DecimalField(blank=True, decimal_places=10, max_digits=22, null=True)),
                ('_file', models.TextField(blank=True, editable=False)),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='orders', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='structure.Project')),
            ],
            options={
                'ordering': ('created',),
                'verbose_name': 'Order',
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name='OrderItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('cost', models.DecimalField(blank=True, decimal_places=10, max_digits=22, null=True)),
                ('limits', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('type', models.PositiveSmallIntegerField(choices=[(1, 'Create'), (2, 'Update'), (3, 'Terminate')], default=1)),
                ('attributes', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('state', django_fsm.FSMIntegerField(choices=[(1, 'pending'), (2, 'executing'), (3, 'done'), (4, 'erred'), (5, 'terminated'), (6, 'terminating')], default=1)),
                ('activated', models.DateTimeField(blank=True, null=True, verbose_name='activation date')),
                ('offering', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='marketplace.Offering')),
            ],
            options={
                'ordering': ('created',),
                'verbose_name': 'Order item',
            },
            bases=(waldur_core.structure.models.StructureLoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Plan',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('unit_price', models.DecimalField(decimal_places=7, default=0, max_digits=22, validators=[django.core.validators.MinValueValidator(Decimal('0'))])),
                ('unit', models.CharField(choices=[('month', 'Per month'), ('half_month', 'Per half month'), ('day', 'Per day'), ('hour', 'Per hour'), ('quantity', 'Quantity')], default='day', max_length=30)),
                ('product_code', models.CharField(blank=True, max_length=30)),
                ('article_code', models.CharField(blank=True, max_length=30)),
                ('object_id', models.PositiveIntegerField(null=True, blank=True)),
                ('archived', models.BooleanField(default=False, help_text='Forbids creation of new resources.')),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('max_amount', models.PositiveSmallIntegerField(blank=True, help_text='Maximum number of plans that could be active. Plan is disabled when maximum amount is reached.', null=True, validators=[django.core.validators.MinValueValidator(1)])),
                ('content_type', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
                ('offering', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='plans', to='marketplace.Offering')),
            ],
            options={
                'ordering': ('name',),
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name='PlanComponent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.PositiveIntegerField(default=0)),
                ('price', models.DecimalField(decimal_places=7, default=0, max_digits=15, validators=[django.core.validators.MinValueValidator(Decimal('0'))], verbose_name='Price per unit per billing period.')),
                ('component', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='components', to='marketplace.OfferingComponent')),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='components', to='marketplace.Plan')),
            ],
        ),
        migrations.CreateModel(
            name='Resource',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('object_id', models.PositiveIntegerField(null=True, blank=True)),
                ('cost', models.DecimalField(blank=True, decimal_places=10, max_digits=22, null=True)),
                ('limits', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('state', django_fsm.FSMIntegerField(choices=[(1, 'Creating'), (2, 'OK'), (3, 'Erred'), (4, 'Updating'), (5, 'Terminating'), (6, 'Terminated')], default=1)),
                ('attributes', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('backend_metadata', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('current_usages', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('content_type', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
                ('offering', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='marketplace.Offering')),
                ('plan', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Plan')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='structure.Project')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.structure.models.StructureLoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='ResourcePlanPeriod',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('start', models.DateTimeField(blank=True, null=True, verbose_name='start')),
                ('end', models.DateTimeField(blank=True, null=True, verbose_name='end')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='marketplace.Plan')),
                ('resource', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='marketplace.Resource')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Screenshot',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('image', models.ImageField(upload_to=waldur_core.media.models.get_upload_path)),
                ('thumbnail', models.ImageField(editable=False, null=True, upload_to=waldur_core.media.models.get_upload_path)),
                ('offering', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='screenshots', to='marketplace.Offering')),
            ],
            options={
                'verbose_name': 'Screenshot',
            },
        ),
        migrations.CreateModel(
            name='Section',
            fields=[
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('key', models.CharField(max_length=255, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=255)),
                ('is_standalone', models.BooleanField(default=False, help_text='Whether section is rendered as a separate tab.')),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='marketplace.Category')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ServiceProvider',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('enable_notifications', models.BooleanField(default=True)),
                ('api_secret_code', models.CharField(blank=True, max_length=255, null=True)),
                ('lead_email', models.EmailField(blank=True, help_text='Email for notification about new request based order items. If this field is set, notifications will be sent.', max_length=254, null=True)),
                ('lead_subject', models.CharField(blank=True, help_text='Notification subject template. Django template variables can be used.', max_length=255)),
                ('lead_body', models.TextField(blank=True, help_text='Notification body template. Django template variables can be used.')),
                ('customer', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='structure.Customer')),
            ],
            options={
                'verbose_name': 'Service provider',
            },
        ),
        migrations.AddField(
            model_name='orderitem',
            name='old_plan',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='marketplace.Plan'),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='order',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='marketplace.Order'),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='plan',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Plan'),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Resource'),
        ),
        migrations.AddField(
            model_name='componentusage',
            name='component',
            field=models.ForeignKey(limit_choices_to={'billing_type': 'usage'}, on_delete=django.db.models.deletion.CASCADE, to='marketplace.OfferingComponent'),
        ),
        migrations.AddField(
            model_name='componentusage',
            name='plan_period',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='components', to='marketplace.ResourcePlanPeriod'),
        ),
        migrations.AddField(
            model_name='componentusage',
            name='resource',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='usages', to='marketplace.Resource'),
        ),
        migrations.AddField(
            model_name='componentquota',
            name='component',
            field=models.ForeignKey(limit_choices_to={'billing_type': 'usage'}, on_delete=django.db.models.deletion.CASCADE, to='marketplace.OfferingComponent'),
        ),
        migrations.AddField(
            model_name='componentquota',
            name='resource',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quotas', to='marketplace.Resource'),
        ),
        migrations.AddField(
            model_name='cartitem',
            name='offering',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='marketplace.Offering'),
        ),
        migrations.AddField(
            model_name='cartitem',
            name='plan',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Plan'),
        ),
        migrations.AddField(
            model_name='cartitem',
            name='project',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='structure.Project'),
        ),
        migrations.AddField(
            model_name='cartitem',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='attribute',
            name='section',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attributes', to='marketplace.Section'),
        ),
        migrations.AddField(
            model_name='aggregateresourcecount',
            name='category',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='marketplace.Category'),
        ),
        migrations.AddField(
            model_name='aggregateresourcecount',
            name='content_type',
            field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType'),
        ),
        migrations.AlterUniqueTogether(
            name='plancomponent',
            unique_together=set([('plan', 'component')]),
        ),
        migrations.AlterUniqueTogether(
            name='offeringcomponent',
            unique_together=set([('type', 'offering')]),
        ),
        migrations.AlterUniqueTogether(
            name='componentusage',
            unique_together=set([('resource', 'component', 'plan_period', 'billing_period')]),
        ),
        migrations.AlterUniqueTogether(
            name='componentquota',
            unique_together=set([('resource', 'component')]),
        ),
        migrations.AlterUniqueTogether(
            name='categorycomponent',
            unique_together=set([('type', 'category')]),
        ),
        migrations.AlterUniqueTogether(
            name='attributeoption',
            unique_together=set([('attribute', 'key')]),
        ),
        migrations.AlterUniqueTogether(
            name='aggregateresourcecount',
            unique_together=set([('category', 'content_type', 'object_id')]),
        ),
    ]
