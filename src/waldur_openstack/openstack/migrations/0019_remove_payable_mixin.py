# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0018_replace_security_group'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='instance',
            name='billing_backend_id',
        ),
        migrations.RemoveField(
            model_name='instance',
            name='last_usage_update_time',
        ),
    ]
