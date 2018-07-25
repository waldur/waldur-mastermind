# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from uuid import uuid4

import django.utils.timezone
from django.db import migrations, models
import model_utils.fields
import waldur_core.core.fields


def init_backup_snapshots(apps, schema_editor):
    Backup = apps.get_model('openstack', 'Backup')
    Snapshot = apps.get_model('openstack', 'Snapshot')
    for backup in Backup.objects.all():
        if backup.metadata.get('system_snapshot_id'):
            Snapshot.objects.create(
                uuid=uuid4().hex,
                size=backup.metadata.get('system_snapshot_size', 0),
                backend_id=backup.metadata.get('system_snapshot_id'),
                tenant=backup.tenant,
                service_project_link=backup.instance.service_project_link,
                name='Backup %s snapshot' % backup.uuid.hex,
                state=3,  # OK state
            )
        if backup.metadata.get('data_snapshot_id'):
            Snapshot.objects.create(
                uuid=uuid4().hex,
                size=backup.metadata.get('data_snapshot_size', 0),
                backend_id=backup.metadata.get('data_snapshot_id'),
                tenant=backup.tenant,
                service_project_link=backup.instance.service_project_link,
                name='Backup %s snapshot' % backup.uuid.hex,
                state=3,  # OK state
            )


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0016_backup_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='backup',
            name='snapshots',
            field=models.ManyToManyField(related_name='backups', to='openstack.Snapshot'),
        ),
        migrations.AlterField(
            model_name='backup',
            name='tenant',
            field=models.ForeignKey(related_name='backups', to='openstack.Tenant'),
        ),
        migrations.RunPython(init_backup_snapshots),
        migrations.CreateModel(
            name='BackupRestoration',
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
        migrations.AlterField(
            model_name='backup',
            name='instance',
            field=models.ForeignKey(related_name='backups', on_delete=django.db.models.deletion.PROTECT, to='openstack.Instance'),
        ),
        migrations.AddField(
            model_name='backuprestoration',
            name='backup',
            field=models.ForeignKey(related_name='restorations', to='openstack.Backup'),
        ),
        migrations.AddField(
            model_name='backuprestoration',
            name='flavor',
            field=models.ForeignKey(related_name='+', to='openstack.Flavor'),
        ),
        migrations.AddField(
            model_name='backuprestoration',
            name='instance',
            field=models.OneToOneField(related_name='+', to='openstack.Instance'),
        ),
        migrations.RenameField(
            model_name='drbackuprestoration',
            old_name='dr_backup',
            new_name='backup',
        ),
    ]
