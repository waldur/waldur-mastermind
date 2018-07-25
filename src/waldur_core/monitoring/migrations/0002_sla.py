# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations

import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0001_initial'),
        ('monitoring', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ResourceItem',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('object_id', models.PositiveIntegerField()),
                ('value', models.FloatField()),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
            ],
            options={
            },
            bases=(models.Model,),
        ),
        migrations.CreateModel(
            name='ResourceSla',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('object_id', models.PositiveIntegerField()),
                ('period', models.CharField(max_length=10)),
                ('value', models.DecimalField(null=True, max_digits=11, decimal_places=4, blank=True)),
                ('agreed_value', models.DecimalField(null=True, max_digits=11, decimal_places=4, blank=True)),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
            ],
            options={
            },
            bases=(models.Model,),
        ),
        migrations.CreateModel(
            name='ResourceSlaStateTransition',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('object_id', models.PositiveIntegerField()),
                ('period', models.CharField(max_length=10)),
                ('timestamp', models.IntegerField()),
                ('state', models.BooleanField(default=False, help_text="If state is True resource became available")),
                ('content_type', models.ForeignKey(to='contenttypes.ContentType')),
            ],
            options={
            },
            bases=(models.Model,),
        ),
        migrations.AlterUniqueTogether(
            name='monitoringitem',
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name='monitoringitem',
            name='content_type',
        ),
        migrations.DeleteModel(
            name='MonitoringItem',
        ),
        migrations.AlterUniqueTogether(
            name='resourceslastatetransition',
            unique_together=set([('timestamp', 'period', 'content_type', 'object_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='resourcesla',
            unique_together=set([('period', 'content_type', 'object_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='resourceitem',
            unique_together=set([('name', 'content_type', 'object_id')]),
        ),
    ]
