# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.logging.loggers
import django.utils.timezone
import model_utils.fields
import waldur_core.core.fields
import waldur_core.core.models
import django.db.models.deletion
import taggit.managers
import django_fsm
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('taggit', '0002_auto_20150616_2121'),
        ('openstack_tenant', '0016_network_subnet_internalip'),
    ]

    operations = [
        migrations.CreateModel(
            name='SnapshotSchedule',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('schedule', waldur_core.core.fields.CronScheduleField(max_length=15, validators=[waldur_core.core.validators.validate_cron_schedule, waldur_core.core.validators.MinCronValueValidator(1)])),
                ('next_trigger_at', models.DateTimeField(null=True)),
                ('timezone', models.CharField(default=django.utils.timezone.get_current_timezone_name, max_length=50)),
                ('is_active', models.BooleanField(default=False)),
                ('state', django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')])),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('start_time', models.DateTimeField(null=True, blank=True)),
                ('retention_time', models.PositiveIntegerField(help_text='Retention time in days, if 0 - resource will be kept forever')),
                ('maximal_number_of_resources', models.PositiveSmallIntegerField()),
                ('call_count', models.PositiveSmallIntegerField(default=0, help_text='How many times a resource schedule was called.')),
                ('service_project_link', models.ForeignKey(related_name='snapshot_schedules', on_delete=django.db.models.deletion.PROTECT, to='openstack_tenant.OpenStackTenantServiceProjectLink')),
                ('source_volume', models.ForeignKey(related_name='snapshot_schedules', to='openstack_tenant.Volume')),
                ('tags', taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.RenameField(
            model_name='backupschedule',
            old_name='maximal_number_of_backups',
            new_name='maximal_number_of_resources',
        ),
        migrations.AddField(
            model_name='snapshot',
            name='kept_until',
            field=models.DateTimeField(help_text='Guaranteed time of snapshot retention. If null - keep forever.', null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='backupschedule',
            name='call_count',
            field=models.PositiveSmallIntegerField(default=0, help_text='How many times a resource schedule was called.'),
        ),
        migrations.AlterField(
            model_name='backupschedule',
            name='retention_time',
            field=models.PositiveIntegerField(help_text='Retention time in days, if 0 - resource will be kept forever'),
        ),
        migrations.AddField(
            model_name='snapshot',
            name='snapshot_schedule',
            field=models.ForeignKey(related_name='snapshots', on_delete=django.db.models.deletion.SET_NULL, blank=True, to='openstack_tenant.SnapshotSchedule', null=True),
        ),
    ]
