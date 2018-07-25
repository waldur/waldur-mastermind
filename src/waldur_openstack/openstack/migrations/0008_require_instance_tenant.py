# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0007_add_instance_tenant'),
    ]

    operations = [
        migrations.AlterField(
            model_name='instance',
            name='tenant',
            field=models.ForeignKey(related_name='instances', to='openstack.Tenant'),
        ),
    ]
