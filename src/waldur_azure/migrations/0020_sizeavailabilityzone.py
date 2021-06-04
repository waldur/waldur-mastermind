# Generated by Django 2.2.20 on 2021-05-27 22:44

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_azure', '0019_add_offer_field'),
    ]

    operations = [
        migrations.CreateModel(
            name='SizeAvailabilityZone',
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
                ('zone', models.PositiveSmallIntegerField()),
                (
                    'location',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_azure.Location',
                    ),
                ),
                (
                    'size',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_azure.Size',
                    ),
                ),
            ],
        ),
    ]