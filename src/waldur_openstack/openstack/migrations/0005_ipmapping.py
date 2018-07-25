# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0035_settings_tags_and_scope'),
        ('openstack', '0004_dr_and_volume_backups'),
    ]

    operations = [
        migrations.CreateModel(
            name='IpMapping',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('public_ip', models.GenericIPAddressField(protocol='IPv4')),
                ('private_ip', models.GenericIPAddressField(protocol='IPv4')),
                ('project', models.ForeignKey(related_name='+', to='structure.Project')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
