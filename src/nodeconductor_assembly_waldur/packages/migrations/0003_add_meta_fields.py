# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0002_openstack_packages'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='openstackpackage',
            options={'verbose_name': 'OpenStack VPC package', 'verbose_name_plural': 'OpenStack VPC packages'},
        ),
        migrations.AlterModelOptions(
            name='packagetemplate',
            options={'verbose_name': 'VPC package template', 'verbose_name_plural': 'VPC package templates'},
        ),
    ]
