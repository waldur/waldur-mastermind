# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0009_add_security_group_tenant'),
    ]

    operations = [
        migrations.AlterField(
            model_name='securitygroup',
            name='tenant',
            field=models.ForeignKey(related_name='security_groups', to='openstack.Tenant'),
        ),
    ]
