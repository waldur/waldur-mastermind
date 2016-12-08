# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from django.conf import settings
import nodeconductor.core.fields
import nodeconductor.core.validators


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('support', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Comment',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('uuid', nodeconductor.core.fields.UUIDField()),
                ('description', models.TextField()),
                ('is_public', models.BooleanField(default=True)),
                ('backend_id', models.CharField(max_length=255, blank=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SupportUser',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[nodeconductor.core.validators.validate_name])),
                ('uuid', nodeconductor.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('user', models.ForeignKey(related_name='+', blank=True, to=settings.AUTH_USER_MODEL, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.RemoveField(
            model_name='issue',
            name='creator',
        ),
        migrations.RemoveField(
            model_name='issue',
            name='key',
        ),
        migrations.AddField(
            model_name='issue',
            name='backend_id',
            field=models.CharField(max_length=255, blank=True),
        ),
        migrations.AddField(
            model_name='issue',
            name='deadline',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='issue',
            name='assignee',
            field=models.ForeignKey(related_name='issues', blank=True, to='support.SupportUser', null=True),
        ),
        migrations.AlterField(
            model_name='issue',
            name='reporter',
            field=models.ForeignKey(related_name='reported_issues', to='support.SupportUser'),
        ),
        migrations.AddField(
            model_name='comment',
            name='author',
            field=models.ForeignKey(related_name='comments', to='support.SupportUser'),
        ),
        migrations.AddField(
            model_name='comment',
            name='issue',
            field=models.ForeignKey(related_name='comments', to='support.Issue'),
        ),
        migrations.AddField(
            model_name='issue',
            name='caller',
            field=models.ForeignKey(related_name='created_issues', default=1, to='support.SupportUser'),
            preserve_default=False,
        ),
    ]
