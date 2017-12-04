# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import model_utils.fields
import waldur_core.core.fields
import django.utils.timezone
from django.conf import settings
import waldur_core.core.validators


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
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('description', models.TextField()),
                ('is_public', models.BooleanField(default=True)),
                ('backend_id', models.CharField(max_length=255, blank=True)),
            ],
            options={
                'ordering': ['-created'],
            },
        ),
        migrations.CreateModel(
            name='SupportUser',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('user', models.ForeignKey(related_name='+', blank=True, to=settings.AUTH_USER_MODEL, null=True)),
            ],
            options={
                'abstract': False,
                'ordering': ['name'],
            },
        ),
        migrations.RemoveField(
            model_name='issue',
            name='creator',
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
            name='key',
            field=models.CharField(max_length=255, blank=True),
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
            field=models.ForeignKey(related_name='created_issues', to='support.SupportUser'),
        ),
    ]
