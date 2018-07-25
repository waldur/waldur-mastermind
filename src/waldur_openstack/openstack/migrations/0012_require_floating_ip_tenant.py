# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0011_add_floating_ip_tenant'),
    ]

    operations = [
        migrations.AlterField(
            model_name='floatingip',
            name='tenant',
            field=models.ForeignKey(related_name='floating_ips', to='openstack.Tenant'),
        ),
    ]
