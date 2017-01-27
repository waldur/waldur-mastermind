# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0009_set_tenant_extra_configuration'),
    ]

    operations = [
        migrations.AddField(
            model_name='packagetemplate',
            name='archived',
            field=models.BooleanField(default=False, help_text='Forbids creation of new packages.'),
        ),
        migrations.AlterField(
            model_name='openstackpackage',
            name='template',
            field=models.ForeignKey(related_name='openstack_packages', on_delete=django.db.models.deletion.PROTECT, to='packages.PackageTemplate', help_text='Tenant will be created based on this template.'),
        ),
    ]
