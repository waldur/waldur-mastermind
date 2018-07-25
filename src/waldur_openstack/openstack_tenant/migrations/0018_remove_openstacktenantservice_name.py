# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0017_snapshot_schedule'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='openstacktenantservice',
            name='name',
        ),
    ]
