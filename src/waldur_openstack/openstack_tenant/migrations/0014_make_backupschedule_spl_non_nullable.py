# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0013_init_backupschedule_spl'),
    ]

    operations = [
        migrations.AlterField(
            model_name='backupschedule',
            name='service_project_link',
            field=models.ForeignKey(related_name='backup_schedules', on_delete=django.db.models.deletion.PROTECT, to='openstack_tenant.OpenStackTenantServiceProjectLink'),
            preserve_default=False,
        ),
    ]
