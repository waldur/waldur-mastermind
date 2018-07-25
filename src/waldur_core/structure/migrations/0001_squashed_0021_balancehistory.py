# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import model_utils.fields
import waldur_core.structure.images
import django.db.models.deletion
import waldur_core.core.fields
import django.core.validators
import django_fsm
import waldur_core.core.validators


class Migration(migrations.Migration):

    #replaces = [('structure', '0001_initial'), ('structure', '0002_customer_native_name'), ('structure', '0003_protect_non_empty_customers'), ('structure', '0004_init_new_quotas'), ('structure', '0005_init_customers_quotas'), ('structure', '0006_inherit_namemixin'), ('structure', '0007_add_service_model'), ('structure', '0008_add_customer_billing_fields'), ('structure', '0009_update_service_models'), ('structure', '0010_add_oracle_service_type'), ('structure', '0011_customer_registration_code'), ('structure', '0012_customer_image'), ('structure', '0013_servicesettings_customer'), ('structure', '0014_servicesettings_options'), ('structure', '0015_drop_service_polymorphic'), ('structure', '0016_init_nc_resource_count_quotas'), ('structure', '0017_add_azure_service_type'), ('structure', '0018_service_settings_plural_form'), ('structure', '0019_rename_nc_service_count_to_nc_service_project_link_count'), ('structure', '0020_servicesettings_certificate'), ('structure', '0021_balancehistory')]

    dependencies = [
        ('auth', '0001_initial'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Customer',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('native_name', models.CharField(default='', max_length=160, blank=True)),
                ('abbreviation', models.CharField(max_length=8, blank=True)),
                ('contact_details', models.TextField(blank=True, validators=[django.core.validators.MaxLengthValidator(500)])),
                ('billing_backend_id', models.CharField(max_length=255, blank=True)),
                ('balance', models.DecimalField(null=True, max_digits=9, decimal_places=3, blank=True)),
                ('image', models.ImageField(null=True, upload_to=waldur_core.structure.images.get_upload_path, blank=True)),
                ('registration_code', models.CharField(default='', max_length=160, blank=True)),
            ],
            options={
                'abstract': False,
                'verbose_name': 'organization',
            },
        ),
        migrations.CreateModel(
            name='CustomerRole',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('role_type', models.SmallIntegerField(choices=[(0, 'Owner')])),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='roles', to='structure.Customer')),
                ('permission_group', models.OneToOneField(to='auth.Group')),
            ],
            options={
                'unique_together': set([('customer', 'role_type')]),
            },
        ),
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='projects', on_delete=django.db.models.deletion.PROTECT, to='structure.Customer')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ProjectRole',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('role_type', models.SmallIntegerField(choices=[(0, 'Administrator'), (1, 'Manager')])),
                ('permission_group', models.OneToOneField(to='auth.Group')),
                ('project', models.ForeignKey(related_name='roles', to='structure.Project')),
            ],
            options={
                'unique_together': set([('project', 'role_type')]),
            },
        ),
        migrations.CreateModel(
            name='ProjectGroup',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='project_groups', on_delete=django.db.models.deletion.PROTECT, to='structure.Customer')),
                ('projects', models.ManyToManyField(related_name='project_groups', to='structure.Project')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ProjectGroupRole',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('role_type', models.SmallIntegerField(choices=[(0, 'Group Manager')])),
                ('permission_group', models.OneToOneField(to='auth.Group')),
                ('project_group', models.ForeignKey(related_name='roles', to='structure.ProjectGroup')),
            ],
            options={
                'unique_together': set([('project_group', 'role_type')]),
            },
        ),
        migrations.CreateModel(
            name='ServiceSettings',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='service_settings', blank=True, to='structure.Customer', null=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('state', django_fsm.FSMIntegerField(default=1, choices=[(1, 'Sync Scheduled'), (2, 'Syncing'), (3, 'In Sync'), (4, 'Erred')])),
                ('backend_url', waldur_core.core.fields.BackendURLField(null=True, blank=True)),
                ('username', models.CharField(max_length=100, null=True, blank=True)),
                ('password', models.CharField(max_length=100, null=True, blank=True)),
                ('certificate', models.FileField(blank=True, null=True, upload_to='certs', validators=[waldur_core.core.validators.FileTypeValidator(allowed_extensions=['pem'], allowed_types=['application/x-pem-file', 'application/x-x509-ca-cert', 'text/plain'])])),
                ('token', models.CharField(max_length=255, null=True, blank=True)),
                ('type', models.SmallIntegerField(choices=[(1, b'OpenStack'), (2, b'DigitalOcean'), (3, b'Amazon'), (4, b'Jira'), (5, b'GitLab'), (6, b'Oracle'), (7, b'Azure')])),
                ('options', waldur_core.core.fields.JSONField(help_text='Extra options', blank=True)),
                ('shared', models.BooleanField(default=False, help_text='Anybody can use it')),
                ('dummy', models.BooleanField(default=False, help_text='Emulate backend operations')),
            ],
            options={
                'abstract': False,
                'verbose_name': 'Service settings',
                'verbose_name_plural': 'Service settings',
            },
        ),
        migrations.CreateModel(
            name='BalanceHistory',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False)),
                ('amount', models.DecimalField(max_digits=9, decimal_places=3)),
                ('customer', models.ForeignKey(verbose_name='organization', to='structure.Customer')),
            ],
        ),
    ]
