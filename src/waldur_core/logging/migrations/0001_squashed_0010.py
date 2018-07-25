# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    replaces = [('logging', '0001_squashed_0003_emailhook_webhook'),
                ('logging', '0001_initial'),
                ('logging', '0002_alert_acknowledged'),
                ('logging', '0002_index_alert_type'),
                ('logging', '0003_emailhook_webhook'),
                ('logging', '0003_add_alert_unique_together_constraint'),
                ('logging', '0004_index_alert_type'),
                ('logging', '0005_add_alert_unique_together_constraint'),
                ('logging', '0006_pushhook_systemnotification'), ('logging', '0007_pushhook_unique_key'),
                ('logging', '0008_pushhook_token'), ('logging', '0009_add_device_metadata'),
                ('logging', '0010_add_event_groups')]

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Alert',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('alert_type', models.CharField(db_index=True, max_length=50)),
                ('message', models.CharField(max_length=255)),
                ('severity', models.SmallIntegerField(choices=[(10, 'Debug'), (20, 'Info'), (30, 'Warning'), (40, 'Error')])),
                ('closed', models.DateTimeField(blank=True, null=True)),
                ('is_closed', models.CharField(blank=True, max_length=32)),
                ('acknowledged', models.BooleanField(default=False)),
                ('context', waldur_core.core.fields.JSONField(blank=True)),
                ('object_id', models.PositiveIntegerField(null=True)),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='contenttypes.ContentType')),
            ],
        ),
        migrations.CreateModel(
            name='EmailHook',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('event_types', waldur_core.core.fields.JSONField(verbose_name='List of event types')),
                ('event_groups', waldur_core.core.fields.JSONField(default=[], verbose_name='List of event groups')),
                ('is_active', models.BooleanField(default=True)),
                ('last_published', models.DateTimeField(default=django.utils.timezone.now)),
                ('email', models.EmailField(max_length=75)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='PushHook',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('event_types', waldur_core.core.fields.JSONField(verbose_name='List of event types')),
                ('event_groups', waldur_core.core.fields.JSONField(default=[], verbose_name='List of event groups')),
                ('is_active', models.BooleanField(default=True)),
                ('last_published', models.DateTimeField(default=django.utils.timezone.now)),
                ('type', models.SmallIntegerField(choices=[(1, 'iOS'), (2, 'Android')])),
                ('device_id', models.CharField(max_length=255, null=True, unique=True)),
                ('device_manufacturer', models.CharField(blank=True, max_length=255, null=True)),
                ('device_model', models.CharField(blank=True, max_length=255, null=True)),
                ('token', models.CharField(max_length=255, null=True, unique=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='SystemNotification',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_types', waldur_core.core.fields.JSONField(verbose_name='List of event types')),
                ('event_groups', waldur_core.core.fields.JSONField(default=[], verbose_name='List of event groups')),
                ('hook_content_type', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='contenttypes.ContentType')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='WebHook',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('event_types', waldur_core.core.fields.JSONField(verbose_name='List of event types')),
                ('event_groups', waldur_core.core.fields.JSONField(default=[], verbose_name='List of event groups')),
                ('is_active', models.BooleanField(default=True)),
                ('last_published', models.DateTimeField(default=django.utils.timezone.now)),
                ('destination_url', models.URLField()),
                ('content_type', models.SmallIntegerField(choices=[(1, 'json'), (2, 'form')], default=1)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AlterUniqueTogether(
            name='pushhook',
            unique_together=set([('user', 'device_id', 'type')]),
        ),
        migrations.AlterUniqueTogether(
            name='alert',
            unique_together=set([('content_type', 'object_id', 'alert_type', 'is_closed')]),
        ),
    ]
