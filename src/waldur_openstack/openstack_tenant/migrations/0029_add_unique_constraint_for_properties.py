# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0052_customer_subnets'),
        ('openstack_tenant', '0028_remove_duplicate_security_groups_networks_subnets'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='network',
            unique_together=set([('settings', 'backend_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='securitygroup',
            unique_together=set([('settings', 'backend_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='subnet',
            unique_together=set([('settings', 'backend_id')]),
        ),
    ]
