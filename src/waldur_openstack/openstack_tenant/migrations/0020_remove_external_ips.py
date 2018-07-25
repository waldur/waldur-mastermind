# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0019_migrate_to_single_external_ip'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='instance',
            name='external_ips',
        ),
    ]
