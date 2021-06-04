# Generated by Django 1.11.16 on 2018-12-20 11:41
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('logging', '0004_json_field'),
    ]

    operations = [
        migrations.CreateModel(
            name='Report',
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
                ('file', models.FileField(upload_to='logging_reports')),
                ('file_size', models.PositiveIntegerField(null=True)),
                (
                    'state',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('done', 'Done'),
                            ('erred', 'Erred'),
                        ],
                        default='pending',
                        max_length=10,
                    ),
                ),
                ('error_message', models.TextField(blank=True)),
            ],
            options={'abstract': False,},
        ),
    ]