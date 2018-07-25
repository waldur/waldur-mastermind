# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0021_volume_instance'),
    ]

    operations = [
        migrations.AddField(
            model_name='volume',
            name='device',
            field=models.CharField(blank=True, max_length=50, help_text='Name of volume as instance device e.g. /dev/vdb.', validators=[django.core.validators.RegexValidator('^/dev/[a-zA-Z0-9]+$', message='Device should match pattern "/dev/alphanumeric+"')]),
        ),
    ]
