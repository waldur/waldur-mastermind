# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
from django.conf import settings
import model_utils.fields
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('logging', '0005_add_alert_unique_together_constraint'),
    ]

    operations = [
        migrations.CreateModel(
            name='PushHook',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('event_types', waldur_core.core.fields.JSONField(verbose_name='List of event types')),
                ('is_active', models.BooleanField(default=True)),
                ('last_published', models.DateTimeField(default=django.utils.timezone.now)),
                ('type', models.SmallIntegerField(choices=[(1, 'iOS'), (2, 'Android')])),
                ('registration_token', models.CharField(max_length=255, blank=True)),
                ('user', models.ForeignKey(to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SystemNotification',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('event_types', waldur_core.core.fields.JSONField(verbose_name='List of event types')),
                ('hook_content_type', models.OneToOneField(related_name='+', to='contenttypes.ContentType')),
            ],
        ),
    ]
