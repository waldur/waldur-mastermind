# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0032_service_setting_backend_url'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='openstackservice',
            name='name',
        ),
    ]
