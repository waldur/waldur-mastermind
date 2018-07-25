# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0012_require_floating_ip_tenant'),
    ]

    operations = [
        migrations.AddField(
            model_name='backupschedule',
            name='backup_type',
            field=models.CharField(default='Regular', max_length=30, choices=[('Regular', 'Regular'), ('DR', 'DR')]),
        ),
        migrations.AddField(
            model_name='backupschedule',
            name='error_message',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='backupschedule',
            name='runtime_state',
            field=models.CharField(max_length=150, verbose_name='runtime state', blank=True),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='backup_schedule',
            field=models.ForeignKey(related_name='dr_backups', on_delete=django.db.models.deletion.SET_NULL, blank=True, to='openstack.BackupSchedule', null=True),
        ),
        migrations.AddField(
            model_name='drbackup',
            name='kept_until',
            field=models.DateTimeField(help_text='Guaranteed time of backup retention. If null - keep forever.', null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='backupschedule',
            name='retention_time',
            field=models.PositiveIntegerField(help_text='Retention time in days, if 0 - backup will be kept forever'),
        ),
    ]
