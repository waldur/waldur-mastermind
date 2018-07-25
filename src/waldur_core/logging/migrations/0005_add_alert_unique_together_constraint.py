# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import uuid

from django.db import models, migrations


def init_is_closed_attribute(apps, schema_editor):
    Alert = apps.get_model('logging', 'Alert')
    for alert in Alert.objects.all():
        if alert.closed is None:
            # Delete duplicates to prevent integrity errors
            (Alert.objects
                .filter(object_id=alert.object_id,
                        content_type=alert.content_type,
                        alert_type=alert.alert_type,
                        closed__isnull=True)
                .delete())
        else:
            alert.is_closed = uuid.uuid4().hex
            alert.save()


class Migration(migrations.Migration):

    #replaces = [('logging', '0003_add_alert_unique_together_constraint')]

    dependencies = [
        ('logging', '0004_index_alert_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='alert',
            name='is_closed',
            field=models.CharField(max_length=32, blank=True),
            preserve_default=True,
        ),
        migrations.RunPython(init_is_closed_attribute),
        migrations.AlterUniqueTogether(
            name='alert',
            unique_together=set([('content_type', 'object_id', 'alert_type', 'is_closed')]),
        ),
    ]
