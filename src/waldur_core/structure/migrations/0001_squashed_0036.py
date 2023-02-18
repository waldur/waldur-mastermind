import django.core.validators
import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
import django_fsm
import model_utils.fields
import netfields.fields
import upload_validator
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.core.validators
import waldur_core.logging.loggers
import waldur_core.media.models
import waldur_core.structure.models


class Migration(migrations.Migration):
    replaces = [
        ('structure', '0001_squashed_0054'),
        ('structure', '0002_immutable_default_json'),
        ('structure', '0003_order_by_name'),
        ('structure', '0004_customer_homepage'),
        ('structure', '0005_customer_domain'),
        ('structure', '0006_customer_backend_id'),
        ('structure', '0007_customer_blocked'),
        ('structure', '0008_customer_division'),
        ('structure', '0009_project_is_removed'),
        ('structure', '0010_customer_geolocation'),
        ('structure', '0011_allow_duplicate_agreement_numbers'),
        ('structure', '0012_customer_sponsor_number'),
        ('structure', '0013_extend_description_limits'),
        ('structure', '0014_remove_customer_type'),
        ('structure', '0015_servicesettings_error_traceback'),
        ('structure', '0016_customerpermissionreview'),
        ('structure', '0017_remove_customer_is_company'),
        ('structure', '0018_servicesettings_is_active'),
        ('structure', '0019_servicesettings_remove_deprecated_fields'),
        ('structure', '0020_drop_servicecertification_model'),
        ('structure', '0021_project_backend_id'),
        ('structure', '0022_project_end_date'),
        ('structure', '0023_add_special_robot_staff_user'),
        ('structure', '0024_project_oecd_fos_2007_code'),
        ('structure', '0025_long_project_name'),
        ('structure', '0026_project_managers'),
        ('structure', '0027_null_boolean_field'),
        ('structure', '0028_project_is_industry'),
        ('structure', '0029_customer_inet'),
        ('structure', '0030_customer_archived'),
        ('structure', '0031_project_image'),
        ('structure', '0032_useragreement'),
        ('structure', '0033_alter_project_customer'),
        ('structure', '0034_notification_notificationtemplate'),
        ('structure', '0035_alter_customer_default_tax_percent'),
        ('structure', '0036_remove_notification_and_notification_template'),
    ]

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('quotas', '0001_squashed_0004'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Customer',
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
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'image',
                    models.ImageField(
                        blank=True,
                        null=True,
                        upload_to=waldur_core.media.models.get_upload_path,
                    ),
                ),
                (
                    'vat_code',
                    models.CharField(blank=True, help_text='VAT number', max_length=20),
                ),
                (
                    'vat_name',
                    models.CharField(
                        blank=True,
                        help_text='Optional business name retrieved for the VAT number.',
                        max_length=255,
                    ),
                ),
                (
                    'vat_address',
                    models.CharField(
                        blank=True,
                        help_text='Optional business address retrieved for the VAT number.',
                        max_length=255,
                    ),
                ),
                ('country', models.CharField(blank=True, max_length=2)),
                (
                    'native_name',
                    models.CharField(blank=True, default='', max_length=160),
                ),
                ('abbreviation', models.CharField(blank=True, max_length=12)),
                (
                    'contact_details',
                    models.TextField(
                        blank=True,
                        validators=[django.core.validators.MaxLengthValidator(500)],
                    ),
                ),
                (
                    'agreement_number',
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    'email',
                    models.EmailField(
                        blank=True, max_length=75, verbose_name='email address'
                    ),
                ),
                (
                    'phone_number',
                    models.CharField(
                        blank=True, max_length=255, verbose_name='phone number'
                    ),
                ),
                (
                    'access_subnets',
                    models.TextField(
                        blank=True,
                        default='',
                        help_text='Enter a comma separated list of IPv4 or IPv6 CIDR addresses from where connection to self-service is allowed.',
                        validators=[waldur_core.core.validators.validate_cidr_list],
                    ),
                ),
                (
                    'registration_code',
                    models.CharField(blank=True, default='', max_length=160),
                ),
                ('address', models.CharField(blank=True, max_length=300)),
                ('postal', models.CharField(blank=True, max_length=20)),
                ('bank_name', models.CharField(blank=True, max_length=150)),
                ('bank_account', models.CharField(blank=True, max_length=50)),
                (
                    'accounting_start_date',
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name='Start date of accounting',
                    ),
                ),
                (
                    'default_tax_percent',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=4,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(200),
                        ],
                    ),
                ),
                ('homepage', models.URLField(blank=True, max_length=255)),
                ('domain', models.CharField(blank=True, max_length=255)),
                (
                    'backend_id',
                    models.CharField(
                        blank=True,
                        help_text='Organization identifier in another application.',
                        max_length=255,
                    ),
                ),
                ('blocked', models.BooleanField(default=False)),
                ('latitude', models.FloatField(blank=True, null=True)),
                ('longitude', models.FloatField(blank=True, null=True)),
                (
                    'sponsor_number',
                    models.PositiveIntegerField(
                        blank=True,
                        help_text='External ID of the sponsor covering the costs',
                        null=True,
                    ),
                ),
                (
                    'inet',
                    netfields.fields.CidrAddressField(
                        blank=True, max_length=43, null=True
                    ),
                ),
                ('archived', models.BooleanField(default=False)),
            ],
            options={
                'verbose_name': 'organization',
                'ordering': ('name',),
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.structure.models.PermissionMixin,
                waldur_core.logging.loggers.LoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='ProjectType',
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
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'ordering': ['name'],
                'verbose_name': 'Project type',
                'verbose_name_plural': 'Project types',
            },
        ),
        migrations.CreateModel(
            name='ServiceSettings',
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
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                (
                    'backend_url',
                    waldur_core.core.fields.BackendURLField(blank=True, null=True),
                ),
                ('username', models.CharField(blank=True, max_length=100, null=True)),
                ('password', models.CharField(blank=True, max_length=100, null=True)),
                ('domain', models.CharField(blank=True, max_length=200, null=True)),
                ('token', models.CharField(blank=True, max_length=255, null=True)),
                (
                    'certificate',
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to='certs',
                        validators=[
                            upload_validator.FileTypeValidator(
                                allowed_extensions=['pem'],
                                allowed_types=[
                                    'application/x-pem-file',
                                    'application/x-x509-ca-cert',
                                    'text/plain',
                                ],
                            )
                        ],
                    ),
                ),
                (
                    'type',
                    models.CharField(
                        db_index=True,
                        max_length=255,
                        validators=[waldur_core.structure.models.validate_service_type],
                    ),
                ),
                (
                    'options',
                    waldur_core.core.fields.JSONField(
                        blank=True, default=dict, help_text='Extra options'
                    ),
                ),
                (
                    'shared',
                    models.BooleanField(default=False, help_text='Anybody can use it'),
                ),
                ('terms_of_services', models.URLField(blank=True, max_length=255)),
                ('object_id', models.PositiveIntegerField(null=True)),
                (
                    'content_type',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to='contenttypes.contenttype',
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='service_settings',
                        to='structure.customer',
                        verbose_name='organization',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'is_active',
                    models.BooleanField(
                        default=True,
                        help_text='Information about inactive service settings will not be updated in the background',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Service settings',
                'verbose_name_plural': 'Service settings',
                'ordering': ('name',),
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name='Project',
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
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=500,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='projects',
                        to='structure.customer',
                        verbose_name='organization',
                    ),
                ),
                (
                    'type',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to='structure.projecttype',
                        verbose_name='project type',
                    ),
                ),
                ('is_removed', models.BooleanField(default=False)),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'end_date',
                    models.DateField(
                        blank=True,
                        help_text='The date is inclusive. Once reached, all project resource will be scheduled for termination.',
                        null=True,
                    ),
                ),
                (
                    'oecd_fos_2007_code',
                    models.CharField(
                        blank=True,
                        choices=[
                            ('1.1', 'Mathematics'),
                            ('1.2', 'Computer and information sciences'),
                            ('1.3', 'Physical sciences'),
                            ('1.4', 'Chemical sciences'),
                            ('1.5', 'Earth and related environmental sciences'),
                            ('1.6', 'Biological sciences'),
                            ('1.7', 'Other natural sciences'),
                            ('2.1', 'Civil engineering'),
                            (
                                '2.2',
                                'Electrical engineering, electronic engineering, information engineering',
                            ),
                            ('2.3', 'Mechanical engineering'),
                            ('2.4', 'Chemical engineering'),
                            ('2.5', 'Materials engineering'),
                            ('2.6', 'Medical engineering'),
                            ('2.7', 'Environmental engineering'),
                            ('2.8', 'Systems engineering'),
                            ('2.9', 'Environmental biotechnology'),
                            ('2.10', 'Industrial biotechnology'),
                            ('2.11', 'Nano technology'),
                            ('2.12', 'Other engineering and technologies'),
                            ('3.1', 'Basic medicine'),
                            ('3.2', 'Clinical medicine'),
                            ('3.3', 'Health sciences'),
                            ('3.4', 'Health biotechnology'),
                            ('3.5', 'Other medical sciences'),
                            ('4.1', 'Agriculture, forestry, and fisheries'),
                            ('4.2', 'Animal and dairy science'),
                            ('4.3', 'Veterinary science'),
                            ('4.4', 'Agricultural biotechnology'),
                            ('4.5', 'Other agricultural sciences'),
                            ('5.1', 'Psychology'),
                            ('5.2', 'Economics and business'),
                            ('5.3', 'Educational sciences'),
                            ('5.4', 'Sociology'),
                            ('5.5', 'Law'),
                            ('5.6', 'Political science'),
                            ('5.7', 'Social and economic geography'),
                            ('5.8', 'Media and communications'),
                            ('5.9', 'Other social sciences'),
                            ('6.1', 'History and archaeology'),
                            ('6.2', 'Languages and literature'),
                            ('6.3', 'Philosophy, ethics and religion'),
                            (
                                '6.4',
                                'Arts (arts, history of arts, performing arts, music)',
                            ),
                            ('6.5', 'Other humanities'),
                        ],
                        max_length=80,
                        null=True,
                    ),
                ),
                ('is_industry', models.BooleanField(default=False)),
                (
                    'image',
                    models.ImageField(
                        blank=True,
                        null=True,
                        upload_to=waldur_core.media.models.get_upload_path,
                    ),
                ),
            ],
            options={
                'abstract': False,
                'base_manager_name': 'objects',
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.structure.models.PermissionMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='PrivateServiceSettings',
            fields=[],
            options={
                'proxy': True,
                'verbose_name_plural': 'Private provider settings',
                'indexes': [],
            },
            bases=('structure.servicesettings',),
        ),
        migrations.CreateModel(
            name='SharedServiceSettings',
            fields=[],
            options={
                'proxy': True,
                'verbose_name_plural': 'Shared provider settings',
                'indexes': [],
            },
            bases=('structure.servicesettings',),
        ),
        migrations.CreateModel(
            name='DivisionType',
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
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'ordering': ('name',),
                'verbose_name': 'division type',
            },
        ),
        migrations.CreateModel(
            name='Division',
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
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'parent',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to='structure.division',
                    ),
                ),
                (
                    'type',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='structure.divisiontype',
                    ),
                ),
            ],
            options={
                'ordering': ('name',),
                'verbose_name': 'division',
            },
        ),
        migrations.AddField(
            model_name='customer',
            name='division',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='structure.division',
            ),
        ),
        migrations.CreateModel(
            name='CustomerPermissionReview',
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
                ('is_pending', models.BooleanField(default=True)),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ('closed', models.DateTimeField(blank=True, null=True)),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='reviews',
                        to='structure.customer',
                        verbose_name='organization',
                    ),
                ),
                (
                    'reviewer',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AlterModelManagers(
            name='project',
            managers=[
                ('available_objects', django.db.models.manager.Manager()),
                ('objects', django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name='CustomerPermission',
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
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ('expiration_time', models.DateTimeField(blank=True, null=True)),
                (
                    'is_active',
                    models.BooleanField(db_index=True, default=True, null=True),
                ),
                (
                    'role',
                    waldur_core.structure.models.CustomerRole(
                        choices=[
                            ('owner', 'Owner'),
                            ('support', 'Support'),
                            ('service_manager', 'Service manager'),
                        ],
                        db_index=True,
                        max_length=30,
                    ),
                ),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='permissions',
                        to='structure.customer',
                        verbose_name='organization',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'unique_together': {('customer', 'role', 'user', 'is_active')},
            },
        ),
        migrations.CreateModel(
            name='ProjectPermission',
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
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ('expiration_time', models.DateTimeField(blank=True, null=True)),
                (
                    'is_active',
                    models.BooleanField(db_index=True, default=True, null=True),
                ),
                (
                    'role',
                    waldur_core.structure.models.ProjectRole(
                        choices=[
                            ('admin', 'Administrator'),
                            ('manager', 'Manager'),
                            ('member', 'Member'),
                        ],
                        db_index=True,
                        max_length=30,
                    ),
                ),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='permissions',
                        to='structure.project',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['-created'],
                'unique_together': {('project', 'role', 'user', 'is_active')},
            },
        ),
        migrations.CreateModel(
            name='UserAgreement',
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
                ('content', models.TextField(blank=True)),
                (
                    'agreement_type',
                    models.CharField(
                        choices=[
                            ('TOS', 'Terms of services'),
                            ('PP', 'Privacy policy'),
                        ],
                        max_length=5,
                        unique=True,
                    ),
                ),
            ],
            options={
                'ordering': ['created'],
            },
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
    ]
