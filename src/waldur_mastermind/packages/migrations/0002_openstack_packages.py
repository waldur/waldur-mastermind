# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0022_volume_device'),
        ('structure', '0037_remove_customer_billing_backend_id'),
        ('packages', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='OpenStackPackage',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('service_settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.RemoveField(
            model_name='packagetemplate',
            name='type',
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='service_settings',
            field=models.ForeignKey(related_name='+', to='structure.ServiceSettings'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='openstackpackage',
            name='template',
            field=models.ForeignKey(related_name='openstack_packages', to='packages.PackageTemplate', help_text='Tenant will be created based on this template.'),
        ),
        migrations.AddField(
            model_name='openstackpackage',
            name='tenant',
            field=models.ForeignKey(related_name='+', to='openstack.Tenant'),
        ),
    ]
