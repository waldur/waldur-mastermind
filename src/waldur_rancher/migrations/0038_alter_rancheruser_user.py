# Generated by Django 4.2.14 on 2024-09-10 14:52

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("waldur_rancher", "0001_squashed_0037_json_field"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rancheruser",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
            ),
        ),
    ]
