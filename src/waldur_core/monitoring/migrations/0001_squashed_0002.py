# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import django.utils.timezone
from django.db import migrations, models

import waldur_core.core.validators


class Migration(migrations.Migration):
    replaces = [('monitoring', '0001_initial'), ('monitoring', '0002_sla')]

    initial = True

    dependencies = [
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ResourceItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name],
                                          verbose_name='name')),
                ('object_id', models.PositiveIntegerField()),
                ('value', models.FloatField()),
                ('content_type',
                 models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
            ],
        ),
        migrations.CreateModel(
            name='ResourceSla',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField()),
                ('period', models.CharField(max_length=10)),
                ('value', models.DecimalField(blank=True, decimal_places=4, max_digits=11, null=True)),
                ('agreed_value', models.DecimalField(blank=True, decimal_places=4, max_digits=11, null=True)),
                ('content_type',
                 models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
            ],
        ),
        migrations.CreateModel(
            name='ResourceSlaStateTransition',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField()),
                ('period', models.CharField(max_length=10)),
                ('timestamp', models.IntegerField()),
                ('state', models.BooleanField(default=False, help_text='If state is True resource became available')),
                ('content_type',
                 models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
            ],
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
