# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    replaces = [('quotas', '0001_initial'),
                ('quotas', '0002_make_quota_scope_nullable'),
                ('quotas', '0003_index_quota_name'),
                ('quotas', '0004_quota_threshold')]

    initial = True

    dependencies = [
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Quota',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('threshold', models.FloatField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('limit', models.FloatField(default=-1)),
                ('usage', models.FloatField(default=0)),
                ('name', models.CharField(db_index=True, max_length=150)),
                ('object_id', models.PositiveIntegerField(null=True)),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
            ],
            bases=(waldur_core.logging.loggers.LoggableMixin, waldur_core.core.models.ReversionMixin, models.Model),
        ),
        migrations.AlterUniqueTogether(
            name='quota',
            unique_together=set([('name', 'content_type', 'object_id')]),
        ),
    ]
