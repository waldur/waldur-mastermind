# Generated by Django 2.2.13 on 2021-02-02 18:44

import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_slurm', '0016_drop_deposit_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='Association',
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
                ('username', models.CharField(max_length=128)),
                (
                    'allocation',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='associations',
                        to='waldur_slurm.Allocation',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
