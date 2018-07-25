# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django_fsm
import waldur_core.core.models
import taggit.managers
import django.core.validators
import waldur_core.logging.loggers
import model_utils.fields
import waldur_core.core.fields
import django.db.models.deletion
import django.utils.timezone
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('taggit', '0002_auto_20150616_2121'),
        ('openstack_tenant', '0002_service_properties'),
    ]

    operations = [
        migrations.CreateModel(
            name='Snapshot',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('runtime_state', models.CharField(max_length=150, verbose_name='runtime state', blank=True)),
                ('state', django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')])),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('start_time', models.DateTimeField(null=True, blank=True)),
                ('size', models.PositiveIntegerField(help_text='Size in MiB')),
                ('metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('service_project_link', models.ForeignKey(related_name='snapshots', on_delete=django.db.models.deletion.PROTECT, to='openstack_tenant.OpenStackTenantServiceProjectLink')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Volume',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('runtime_state', models.CharField(max_length=150, verbose_name='runtime state', blank=True)),
                ('state', django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')])),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('start_time', models.DateTimeField(null=True, blank=True)),
                ('size', models.PositiveIntegerField(help_text='Size in MiB')),
                ('device', models.CharField(blank=True, help_text=b'Name of volume as instance device e.g. /dev/vdb.', max_length=50, validators=[django.core.validators.RegexValidator(b'^/dev/[a-zA-Z0-9]+$', message=b'Device should match pattern "/dev/alphanumeric+"')])),
                ('bootable', models.BooleanField(default=False)),
                ('metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('image_metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('type', models.CharField(max_length=100, blank=True)),
                ('image', models.ForeignKey(to='openstack_tenant.Image', blank=True, null=True)),
                ('service_project_link', models.ForeignKey(related_name='volumes', on_delete=django.db.models.deletion.PROTECT, to='openstack_tenant.OpenStackTenantServiceProjectLink')),
                ('source_snapshot', models.ForeignKey(related_name='volumes', on_delete=django.db.models.deletion.SET_NULL, to='openstack_tenant.Snapshot', blank=True, null=True)),
                ('tags', taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.AddField(
            model_name='snapshot',
            name='source_volume',
            field=models.ForeignKey(related_name='snapshots', on_delete=django.db.models.deletion.PROTECT, to='openstack_tenant.Volume', null=True),
        ),
        migrations.AddField(
            model_name='snapshot',
            name='tags',
            field=taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags'),
        ),
    ]
