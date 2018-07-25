# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.logging.loggers
import model_utils.fields
import waldur_core.core.fields
import waldur_core.structure.models
import waldur_core.core.models
import django.db.models.deletion
import django.utils.timezone
import taggit.managers
import django_fsm
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('taggit', '0002_auto_20150616_2121'),
        ('openstack_tenant', '0003_volume_and_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='Instance',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('latitude', models.FloatField(null=True, blank=True)),
                ('longitude', models.FloatField(null=True, blank=True)),
                ('runtime_state', models.CharField(max_length=150, verbose_name='runtime state', blank=True)),
                ('state', django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')])),
                ('cores', models.PositiveSmallIntegerField(default=0, help_text='Number of cores in a VM')),
                ('ram', models.PositiveIntegerField(default=0, help_text='Memory size in MiB')),
                ('disk', models.PositiveIntegerField(default=0, help_text='Disk size in MiB')),
                ('min_ram', models.PositiveIntegerField(default=0, help_text='Minimum memory size in MiB')),
                ('min_disk', models.PositiveIntegerField(default=0, help_text='Minimum disk size in MiB')),
                ('external_ips', models.GenericIPAddressField(null=True, protocol='IPv4', blank=True)),
                ('internal_ips', models.GenericIPAddressField(null=True, protocol='IPv4', blank=True)),
                ('image_name', models.CharField(max_length=150, blank=True)),
                ('key_name', models.CharField(max_length=50, blank=True)),
                ('key_fingerprint', models.CharField(max_length=47, blank=True)),
                ('user_data', models.TextField(help_text='Additional data that will be added to instance on provisioning', blank=True)),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('start_time', models.DateTimeField(null=True, blank=True)),
                ('flavor_name', models.CharField(max_length=255, blank=True)),
                ('flavor_disk', models.PositiveIntegerField(default=0, help_text=b'Flavor disk size in MiB')),
                ('security_groups', models.ManyToManyField(related_name='instances', to='openstack_tenant.SecurityGroup')),
                ('service_project_link', models.ForeignKey(related_name='instances', on_delete=django.db.models.deletion.PROTECT, to='openstack_tenant.OpenStackTenantServiceProjectLink')),
                ('tags', taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.AddField(
            model_name='volume',
            name='instance',
            field=models.ForeignKey(related_name='volumes', blank=True, to='openstack_tenant.Instance', null=True),
        ),
    ]
