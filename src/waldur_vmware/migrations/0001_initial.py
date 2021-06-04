# Generated by Django 1.11.20 on 2019-06-18 11:00
import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.logging.loggers
import waldur_core.structure.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('structure', '0009_project_is_removed'),
    ]

    operations = [
        migrations.CreateModel(
            name='VMwareService',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'available_for_all',
                    models.BooleanField(
                        default=False,
                        help_text='Service will be automatically added to all customers projects if it is available for all',
                    ),
                ),
                (
                    'customer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='structure.Customer',
                        verbose_name='organization',
                    ),
                ),
            ],
            options={
                'verbose_name': 'VMware provider',
                'verbose_name_plural': 'VMware providers',
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='VMwareServiceProjectLink',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='structure.Project',
                    ),
                ),
                (
                    'service',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_vmware.VMwareService',
                    ),
                ),
            ],
            options={
                'abstract': False,
                'verbose_name': 'VMware provider project link',
                'verbose_name_plural': 'VMware provider project links',
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.logging.loggers.LoggableMixin,
                models.Model,
            ),
        ),
        migrations.AddField(
            model_name='vmwareservice',
            name='projects',
            field=models.ManyToManyField(
                related_name='_vmwareservice_projects_+',
                through='waldur_vmware.VMwareServiceProjectLink',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='vmwareservice',
            name='settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to='structure.ServiceSettings',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='vmwareserviceprojectlink',
            unique_together=set([('service', 'project')]),
        ),
        migrations.AlterUniqueTogether(
            name='vmwareservice', unique_together=set([('customer', 'settings')]),
        ),
    ]