import re

import django.contrib.auth.models
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators
import waldur_core.logging.loggers
import waldur_core.media.models


class Migration(migrations.Migration):

    replaces = [
        ('core', '0001_squashed_0008'),
        ('core', '0002_remove_organization'),
        ('core', '0003_enlarge_username'),
        ('core', '0004_user_details'),
        ('core', '0005_user_backend_id'),
        ('core', '0006_extend_organization'),
        ('core', '0007_changeemailrequest'),
        ('core', '0008_changeemailrequest_uuid'),
        ('core', '0009_changeemailrequest_uuid_populate'),
        ('core', '0010_changeemailrequest_uuid_unique'),
        ('core', '0011_extend_description_limits'),
        ('core', '0012_drop_slurm_packages'),
        ('core', '0013_user_is_identity_manager'),
        ('core', '0014_user_affiliations'),
        ('core', '0015_user_first_and_last_name'),
        ('core', '0016_clean_affiliations'),
        ('core', '0017_affiliations_default'),
        ('core', '0018_drop_leftover_tables'),
        ('core', '0019_drop_zabbix_tables'),
        ('core', '0020_feature'),
        ('core', '0021_user_last_sync'),
        ('core', '0022_long_email'),
        ('core', '0023_query_field'),
        ('core', '0024_query_field_fix'),
        ('core', '0025_user_notifications_enabled'),
        ('core', '0026_json_field'),
        ('core', '0027_alter_user_job_title'),
        ('core', '0028_user_image'),
        ('core', '0029_notification_notificationtemplate'),
    ]

    initial = True

    dependencies = [
        ('auth', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
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
                ('password', models.CharField(max_length=128, verbose_name='password')),
                (
                    'last_login',
                    models.DateTimeField(
                        blank=True, null=True, verbose_name='last login'
                    ),
                ),
                (
                    'is_superuser',
                    models.BooleanField(
                        default=False,
                        help_text='Designates that this user has all permissions without explicitly assigning them.',
                        verbose_name='superuser status',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'username',
                    models.CharField(
                        help_text='Required. 128 characters or fewer. Letters, numbers and @/./+/-/_ characters',
                        max_length=128,
                        unique=True,
                        validators=[
                            django.core.validators.RegexValidator(
                                re.compile('^[\\w.@+-]+$'),
                                'Enter a valid username.',
                                'invalid',
                            )
                        ],
                        verbose_name='username',
                    ),
                ),
                (
                    'civil_number',
                    models.CharField(
                        blank=True,
                        default=None,
                        max_length=50,
                        null=True,
                        unique=True,
                        verbose_name='civil number',
                    ),
                ),
                (
                    'native_name',
                    models.CharField(
                        blank=True, max_length=100, verbose_name='native name'
                    ),
                ),
                (
                    'phone_number',
                    models.CharField(
                        blank=True, max_length=255, verbose_name='phone number'
                    ),
                ),
                (
                    'organization',
                    models.CharField(
                        blank=True, max_length=255, verbose_name='organization'
                    ),
                ),
                (
                    'job_title',
                    models.CharField(
                        blank=True, max_length=120, verbose_name='job title'
                    ),
                ),
                (
                    'email',
                    models.EmailField(
                        blank=True, max_length=320, verbose_name='email address'
                    ),
                ),
                (
                    'is_staff',
                    models.BooleanField(
                        default=False,
                        help_text='Designates whether the user can log into this admin site.',
                        verbose_name='staff status',
                    ),
                ),
                (
                    'is_active',
                    models.BooleanField(
                        default=True,
                        help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.',
                        verbose_name='active',
                    ),
                ),
                (
                    'is_support',
                    models.BooleanField(
                        default=False,
                        help_text='Designates whether the user is a global support user.',
                        verbose_name='support status',
                    ),
                ),
                (
                    'date_joined',
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name='date joined'
                    ),
                ),
                (
                    'registration_method',
                    models.CharField(
                        blank=True,
                        default='default',
                        help_text='Indicates what registration method were used.',
                        max_length=50,
                        verbose_name='registration method',
                    ),
                ),
                (
                    'agreement_date',
                    models.DateTimeField(
                        blank=True,
                        help_text='Indicates when the user has agreed with the policy.',
                        null=True,
                        verbose_name='agreement date',
                    ),
                ),
                ('preferred_language', models.CharField(blank=True, max_length=10)),
                ('competence', models.CharField(blank=True, max_length=255)),
                (
                    'token_lifetime',
                    models.PositiveIntegerField(
                        help_text='Token lifetime in seconds.',
                        null=True,
                        validators=[django.core.validators.MinValueValidator(60)],
                    ),
                ),
                (
                    'groups',
                    models.ManyToManyField(
                        blank=True,
                        help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.',
                        related_name='user_set',
                        related_query_name='user',
                        to='auth.Group',
                        verbose_name='groups',
                    ),
                ),
                (
                    'user_permissions',
                    models.ManyToManyField(
                        blank=True,
                        help_text='Specific permissions for this user.',
                        related_name='user_set',
                        related_query_name='user',
                        to='auth.Permission',
                        verbose_name='user permissions',
                    ),
                ),
                (
                    'details',
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='Extra details from authentication backend.',
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'is_identity_manager',
                    models.BooleanField(
                        default=False,
                        help_text='Designates whether the user is allowed to manage remote user identities.',
                    ),
                ),
                (
                    'affiliations',
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Person's affiliation within organization such as student, faculty, staff.",
                    ),
                ),
                (
                    'first_name',
                    models.CharField(
                        blank=True, max_length=100, verbose_name='first name'
                    ),
                ),
                (
                    'last_name',
                    models.CharField(
                        blank=True, max_length=100, verbose_name='last name'
                    ),
                ),
                (
                    'last_sync',
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ('query_field', models.CharField(blank=True, max_length=300)),
                (
                    'notifications_enabled',
                    models.BooleanField(
                        default=True,
                        help_text='Designates whether the user is allowed to receive email notifications.',
                    ),
                ),
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
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
                'ordering': ['username'],
            },
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model),
            managers=[
                ('objects', django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name='SshPublicKey',
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
                ('name', models.CharField(blank=True, max_length=150)),
                ('fingerprint', models.CharField(max_length=47)),
                (
                    'public_key',
                    models.TextField(
                        validators=[
                            django.core.validators.MaxLengthValidator(2000),
                            waldur_core.core.validators.validate_ssh_public_key,
                        ]
                    ),
                ),
                ('is_shared', models.BooleanField(default=False)),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'verbose_name': 'SSH public key',
                'verbose_name_plural': 'SSH public keys',
                'ordering': ['name'],
                'unique_together': {('user', 'name')},
            },
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='ChangeEmailRequest',
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
                ('email', models.EmailField(max_length=254)),
                (
                    'user',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'verbose_name': 'change email request',
                'verbose_name_plural': 'change email requests',
            },
        ),
        migrations.CreateModel(
            name='Feature',
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
                ('key', models.TextField(max_length=255, unique=True)),
                ('value', models.BooleanField(default=False)),
            ],
        ),
        migrations.CreateModel(
            name='NotificationTemplate',
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
                    'path',
                    models.CharField(
                        help_text="Example: 'flatpages/default.html'",
                        max_length=150,
                        verbose_name='path',
                    ),
                ),
            ],
            options={
                'ordering': ['name', 'path'],
            },
        ),
        migrations.CreateModel(
            name='Notification',
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
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('key', models.CharField(max_length=255, unique=True)),
                (
                    'enabled',
                    models.BooleanField(
                        default=True,
                        help_text='Indicates if notification is enabled or disabled',
                    ),
                ),
                ('templates', models.ManyToManyField(to='core.NotificationTemplate')),
            ],
            options={
                'ordering': ['key'],
            },
        ),
    ]
