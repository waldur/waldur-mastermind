# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import django.contrib.postgres.fields.jsonb
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('structure', '0003_order_by_name'),
        ('marketplace', '0044_cartitem'),
    ]

    operations = [
        migrations.CreateModel(
            name='Resource',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('object_id', models.PositiveIntegerField(null=True)),
                ('attributes', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
                ('plan', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Plan')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='structure.Project')),
                ('limits', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict)),
                ('state', django_fsm.FSMIntegerField(choices=[(1, 'Creating'), (2, 'OK'), (3, 'Erred'), (4, 'Updating'), (5, 'Terminating'), (6, 'Terminated')], default=1)),
                ('offering', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='marketplace.Offering')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified'))
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='componentquota',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Resource'),
        ),
        migrations.AddField(
            model_name='componentusage',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='marketplace.Resource'),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                    to='marketplace.Resource'),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='limits',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=dict, null=True),
        ),
    ]
