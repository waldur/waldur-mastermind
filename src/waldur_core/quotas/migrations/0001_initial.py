# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Quota',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('name', models.CharField(max_length=150, verbose_name='name')),
                ('limit', models.FloatField(default=-1)),
                ('usage', models.FloatField(default=0)),
                ('object_id', models.PositiveIntegerField()),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
            ],
            options={
            },
            bases=(models.Model,),
        ),
        migrations.AlterUniqueTogether(
            name='quota',
            unique_together=set([('name', 'content_type', 'object_id')]),
        ),
    ]
