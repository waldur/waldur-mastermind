# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0010_rename_floating_ip_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='backupschedule',
            name='call_count',
            field=models.PositiveSmallIntegerField(default=0, help_text='How many times backup schedule was called.'),
        ),
    ]
