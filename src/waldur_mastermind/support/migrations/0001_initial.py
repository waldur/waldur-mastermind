# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
from django.conf import settings
import model_utils.fields
import waldur_core.core.fields
import waldur_core.structure.models


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('structure', '0037_remove_customer_billing_backend_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='Issue',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('key', models.CharField(max_length=255)),
                ('type', models.CharField(default='informational', max_length=30, choices=[('informational', 'Informational'), ('service_request', 'Service request'), ('change_request', 'Change request'), ('incident', 'Incident')])),
                ('summary', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True)),
                ('status', models.CharField(max_length=255)),
                ('resolution', models.CharField(max_length=255, blank=True)),
                ('resource_object_id', models.PositiveIntegerField(null=True)),
                ('assignee', models.ForeignKey(related_name='assigned_issues', blank=True, to=settings.AUTH_USER_MODEL, null=True)),
                ('creator', models.ForeignKey(related_name='created_issues', to=settings.AUTH_USER_MODEL)),
                ('customer', models.ForeignKey(verbose_name='organization', related_name='issues', blank=True, to='structure.Customer', null=True)),
                ('project', models.ForeignKey(related_name='issues', blank=True, to='structure.Project', null=True)),
                ('reporter', models.ForeignKey(related_name='reported_issues', to=settings.AUTH_USER_MODEL)),
                ('resource_content_type', models.ForeignKey(to='contenttypes.ContentType', null=True)),
            ],
            options={
                'ordering': ['-created'],
            },
            bases=(models.Model, waldur_core.structure.models.StructureLoggableMixin),
        ),
    ]
