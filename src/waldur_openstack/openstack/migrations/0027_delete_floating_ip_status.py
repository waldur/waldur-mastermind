# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import model_utils.fields
import taggit.managers
import django_fsm
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0026_floating_ip_resource'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='floatingip',
            name='status',
        ),
    ]
