# Generated by Django 3.2.15 on 2022-09-23 14:09

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('structure', '0032_useragreement'),
    ]

    operations = [
        migrations.AlterField(
            model_name='project',
            name='customer',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='projects',
                to='structure.customer',
                verbose_name='organization',
            ),
        ),
    ]
