# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.logging.loggers
import django_fsm
import waldur_core.core.models
import django.db.models.deletion
import django.utils.timezone
import waldur_core.core.fields
import taggit.managers
import model_utils.fields
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('taggit', '0002_auto_20150616_2121'),
        ('openstack', '0003_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='DRBackup',
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
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='VolumeBackup',
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
                ('metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('service_project_link', models.ForeignKey(related_name='volume_backups', on_delete=django.db.models.deletion.PROTECT, to='openstack.OpenStackServiceProjectLink')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.RemoveField(
            model_name='snapshot',
            name='volume',
        ),
        migrations.AddField(
            model_name='snapshot',
            name='source_volume',
            field=models.ForeignKey(related_name='snapshots', on_delete=django.db.models.deletion.PROTECT, default=1, to='openstack.Volume'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='snapshot',
            name='tenant',
            field=models.ForeignKey(related_name='snapshots', default=1, to='openstack.Tenant'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='volume',
            name='source_snapshot',
            field=models.ForeignKey(related_name='volumes', on_delete=django.db.models.deletion.SET_NULL, to='openstack.Snapshot', null=True),
        ),
        migrations.AddField(
            model_name='volumebackup',
            name='source_volume',
            field=models.ForeignKey(related_name='backups', on_delete=django.db.models.deletion.SET_NULL, to='openstack.Volume', null=True),
        ),
        migrations.AddField(
            model_name='volumebackup',
            name='tags',
            field=taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags'),
        ),
        migrations.AddField(
            model_name='volumebackup',
            name='tenant',
            field=models.ForeignKey(related_name='volume_backups', to='openstack.Tenant'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='instance_volumes',
            field=models.ManyToManyField(related_name='_drbackup_instance_volumes_+', to='openstack.Volume'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='service_project_link',
            field=models.ForeignKey(related_name='dr_backups', on_delete=django.db.models.deletion.PROTECT, to='openstack.OpenStackServiceProjectLink'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='source_instance',
            field=models.ForeignKey(related_name='dr_backups', on_delete=django.db.models.deletion.SET_NULL, to='openstack.Instance', null=True),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='tags',
            field=taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='temporary_snapshots',
            field=models.ManyToManyField(related_name='_drbackup_temporary_snapshots_+', to='openstack.Snapshot'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='temporary_volumes',
            field=models.ManyToManyField(related_name='_drbackup_temporary_volumes_+', to='openstack.Volume'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='tenant',
            field=models.ForeignKey(related_name='dr_backups', to='openstack.Tenant'),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='volume_backups',
            field=models.ManyToManyField(related_name='dr_backups', to='openstack.VolumeBackup'),
        ),
    ]
