# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import django.utils.timezone
import model_utils.fields
from django.db import models, migrations

import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='MonitoringItem',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('object_id', models.PositiveIntegerField()),
                ('value', models.CharField(max_length=255, blank=True)),
                ('last_updated', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False)),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
            ],
            options={
            },
            bases=(models.Model,),
        ),
        migrations.AlterUniqueTogether(
            name='monitoringitem',
            unique_together=set([('name', 'content_type', 'object_id')]),
        ),
    ]
