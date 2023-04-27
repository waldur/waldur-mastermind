# Generated by Django 3.2.18 on 2023-03-09 14:32

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('marketplace_remote', '0004_projectupdaterequest_is_industry'),
    ]

    operations = [
        migrations.AddField(
            model_name='projectupdaterequest',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]