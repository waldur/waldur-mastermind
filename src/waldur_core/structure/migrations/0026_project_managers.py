# Generated by Django 2.2.24 on 2022-01-06 16:20

import django.db.models.manager
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0025_long_project_name'),
    ]

    operations = [
        migrations.AlterModelManagers(
            name='project',
            managers=[
                ('available_objects', django.db.models.manager.Manager()),
                ('objects', django.db.models.manager.Manager()),
            ],
        ),
    ]