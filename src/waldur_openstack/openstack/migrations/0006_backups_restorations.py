# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0005_ipmapping'),
    ]

    operations = [
        migrations.CreateModel(
            name='DRBackupRestoration',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('runtime_state', models.CharField(max_length=150, verbose_name='runtime state', blank=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='VolumeBackupRecord',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('service', models.CharField(max_length=200)),
                ('details', waldur_core.core.fields.JSONField(blank=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='VolumeBackupRestoration',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='drbackup',
            name='metadata',
            field=waldur_core.core.fields.JSONField(help_text='Information about instance that will be used on restoration', blank=True),
        ),
        migrations.AddField(
            model_name='volumebackup',
            name='size',
            field=models.PositiveIntegerField(default=0, help_text='Size of source volume in MiB'),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='snapshot',
            name='source_volume',
            field=models.ForeignKey(related_name='snapshots', on_delete=django.db.models.deletion.SET_NULL, to='openstack.Volume', null=True),
        ),
        migrations.AlterField(
            model_name='volumebackup',
            name='metadata',
            field=waldur_core.core.fields.JSONField(help_text='Information about volume that will be used on restoration', blank=True),
        ),
        migrations.AddField(
            model_name='volumebackuprestoration',
            name='mirorred_volume_backup',
            field=models.ForeignKey(related_name='+', on_delete=django.db.models.deletion.SET_NULL, to='openstack.VolumeBackup', null=True),
        ),
        migrations.AddField(
            model_name='volumebackuprestoration',
            name='tenant',
            field=models.ForeignKey(related_name='volume_backup_restorations', to='openstack.Tenant'),
        ),
        migrations.AddField(
            model_name='volumebackuprestoration',
            name='volume',
            field=models.OneToOneField(related_name='+', to='openstack.Volume'),
        ),
        migrations.AddField(
            model_name='volumebackuprestoration',
            name='volume_backup',
            field=models.ForeignKey(related_name='restorations', to='openstack.VolumeBackup'),
        ),
        migrations.AddField(
            model_name='drbackuprestoration',
            name='dr_backup',
            field=models.ForeignKey(related_name='restorations', to='openstack.DRBackup'),
        ),
        migrations.AddField(
            model_name='drbackuprestoration',
            name='flavor',
            field=models.ForeignKey(related_name='+', to='openstack.Flavor'),
        ),
        migrations.AddField(
            model_name='drbackuprestoration',
            name='instance',
            field=models.OneToOneField(related_name='+', to='openstack.Instance'),
        ),
        migrations.AddField(
            model_name='drbackuprestoration',
            name='tenant',
            field=models.ForeignKey(related_name='+', to='openstack.Tenant', help_text='Tenant for instance restoration'),
        ),
        migrations.AddField(
            model_name='drbackuprestoration',
            name='volume_backup_restorations',
            field=models.ManyToManyField(related_name='_drbackuprestoration_volume_backup_restorations_+', to='openstack.VolumeBackupRestoration'),
        ),
        migrations.AddField(
            model_name='volumebackup',
            name='record',
            field=models.ForeignKey(related_name='volume_backups', on_delete=django.db.models.deletion.SET_NULL, to='openstack.VolumeBackupRecord', null=True),
        ),
    ]
