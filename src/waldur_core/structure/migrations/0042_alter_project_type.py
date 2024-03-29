# Generated by Django 4.2.10 on 2024-02-23 17:03

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0041_delete_old_permissions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="structure.projecttype",
                verbose_name="project type",
            ),
        ),
    ]
