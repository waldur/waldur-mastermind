# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0009_tenant_service_verbose_name'),
    ]

    operations = [
        migrations.RenameField(
            model_name='floatingip',
            old_name='status',
            new_name='runtime_state',
        ),
    ]
