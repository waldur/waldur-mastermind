# Generated by Django 3.2.18 on 2023-06-22 19:59

import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('structure', '0039_project_end_date_requested_by'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectEstimatedCostPolicy',
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
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('has_fired', models.BooleanField(default=False)),
                (
                    'actions',
                    models.CharField(max_length=255),
                ),
                ('limit_cost', models.IntegerField()),
                (
                    'project',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='structure.project',
                    ),
                ),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'verbose_name_plural': 'Project estimated cost policies',
            },
        ),
    ]
