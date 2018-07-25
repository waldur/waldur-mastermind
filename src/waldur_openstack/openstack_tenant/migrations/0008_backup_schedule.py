# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import waldur_core.logging.loggers
import django.utils.timezone
import waldur_core.core.fields
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0007_backup_backuprestoration'),
    ]

    operations = [
        migrations.CreateModel(
            name='BackupSchedule',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('schedule', waldur_core.core.fields.CronScheduleField(max_length=15, validators=[waldur_core.core.validators.validate_cron_schedule, waldur_core.core.validators.MinCronValueValidator(1)])),
                ('next_trigger_at', models.DateTimeField(null=True)),
                ('timezone', models.CharField(default=django.utils.timezone.get_current_timezone_name, max_length=50)),
                ('is_active', models.BooleanField(default=False)),
                ('retention_time', models.PositiveIntegerField(help_text=b'Retention time in days, if 0 - backup will be kept forever')),
                ('maximal_number_of_backups', models.PositiveSmallIntegerField()),
                ('instance', models.ForeignKey(related_name='backup_schedules', to='openstack_tenant.Instance')),
            ],
            options={
                'abstract': False,
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.AddField(
            model_name='backup',
            name='backup_schedule',
            field=models.ForeignKey(related_name='backups', on_delete=django.db.models.deletion.SET_NULL, blank=True, to='openstack_tenant.BackupSchedule', null=True),
        ),
    ]
