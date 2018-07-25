# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import model_utils.fields
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0014_make_backupschedule_spl_non_nullable'),
    ]

    operations = [
        migrations.CreateModel(
            name='SnapshotRestoration',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('snapshot', models.ForeignKey(related_name='restorations', to='openstack_tenant.Snapshot')),
                ('volume', models.OneToOneField(related_name='restoration', to='openstack_tenant.Volume')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
