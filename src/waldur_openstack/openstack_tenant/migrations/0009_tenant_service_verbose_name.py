# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0008_backup_schedule'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='openstacktenantservice',
            options={'verbose_name': 'OpenStackTenant provider', 'verbose_name_plural': 'OpenStackTenant providers'},
        ),
    ]
