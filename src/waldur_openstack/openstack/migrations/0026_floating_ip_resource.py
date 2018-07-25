# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import model_utils.fields
import taggit.managers
import django_fsm
import waldur_core.core.validators


def migrate_floatingip_status(apps, schema_editor):
    FloatingIP = apps.get_model('openstack', 'FloatingIP')

    for floating_ip in FloatingIP.objects.all():
        floating_ip.runtime_state = floating_ip.status or 'DOWN'
        floating_ip.save()


def migrate_floatingip_name(apps, schema_editor):
    FloatingIP = apps.get_model('openstack', 'FloatingIP')

    for floating_ip in FloatingIP.objects.all():
        floating_ip.name = '' if floating_ip.address is None else str(floating_ip.address)
        floating_ip.save()


class Migration(migrations.Migration):

    dependencies = [
        ('taggit', '0002_auto_20150616_2121'),
        ('openstack', '0025_delete_tenant_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='floatingip',
            name='created',
            field=model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='description',
            field=models.CharField(max_length=500, verbose_name='description', blank=True),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='error_message',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='modified',
            field=model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='name',
            field=models.CharField(default='', max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name]),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='floatingip',
            name='runtime_state',
            field=models.CharField(max_length=150, verbose_name='runtime state', blank=True),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='start_time',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='state',
            field=django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')]),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='tags',
            field=taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags'),
        ),
        migrations.AlterField(
            model_name='floatingip',
            name='address',
            field=models.GenericIPAddressField(null=True, protocol='IPv4', blank=True),
        ),
        migrations.AlterField(
            model_name='floatingip',
            name='backend_id',
            field=models.CharField(max_length=255, blank=True),
        ),
        migrations.RunPython(migrate_floatingip_status),
        migrations.RunPython(migrate_floatingip_name),
    ]
